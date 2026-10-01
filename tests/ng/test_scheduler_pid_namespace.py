"""PID-namespace lease contracts: real unshare plus in-process twins.

Real kernel namespaces (no process-table fixtures): a recorder inside
``unshare --pid`` writes pids that mean nothing in the observer's /proc.
The observer must judge those rows by the lease lock alone — never trust
the foreign pids, never hold a dead run's slots, never release a live
foreign lease. Every real-namespace case has an in-process equivalent
(observer identity monkeypatched), so coverage never depends on unshare.

Namespace-init cleanup SIGKILLs the init (found as the child of the
unshare process via psutil); ``--kill-child`` is the backstop. The
child's JSON line is read with ``select`` under a bounded timeout.
"""
from __future__ import annotations

import contextlib
import json
import os
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import psutil
import pytest

from ptest import contracts as C
from ptest import leases
from ptest import platform
from ptest import scheduler

_WATCHDOG_S = 30.0

_CHILD = textwrap.dedent("""\
    import json, os, subprocess, sys, time
    from pathlib import Path
    from ptest import contracts as C, platform, scheduler

    os.setsid()  # the namespace init has pgid 0 until it leads a session
    spec = json.loads(sys.argv[1])
    domain = C.DomainPaths(root=Path(spec["root"]),
                           machine_config=Path(spec["machine_config"]),
                           ledger=Path(spec["ledger"]), marker=Path(spec["marker"]),
                           fixture=spec["fixture"], domain_id=spec["domain_id"])
    owner = platform.process_identity(os.getpid())
    checkout = C.CheckoutIdentity(project_id="1" * 32, checkout_id="2" * 32,
                                  root=Path(spec["root"]) / "foreign")
    ticket = scheduler.enqueue(domain, C.AdmissionRequest(
        run_id="a" * 32, checkout=checkout, owner=owner, slots=1,
        exclusive=False, fixture=spec["fixture"]))
    grant = scheduler.poll(domain, ticket).grant
    guard_proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
    guard = platform.process_identity(guard_proc.pid)
    assert scheduler.register_guard(domain, grant, guard)
    print(json.dumps({"owner_pid": owner.pid, "guard_pid": guard.pid,
                      "guard_pgid": guard.pgid}), flush=True)
    time.sleep(300)
    """)

_OBSERVER = textwrap.dedent("""\
    import json, sys
    from pathlib import Path
    from ptest import contracts as C, scheduler

    spec = json.loads(sys.argv[1])
    domain = C.DomainPaths(root=Path(spec["root"]),
                           machine_config=Path(spec["machine_config"]),
                           ledger=Path(spec["ledger"]), marker=Path(spec["marker"]),
                           fixture=spec["fixture"], domain_id=spec["domain_id"])
    print(json.dumps({v.run_id: v.state.value for v in scheduler.reconcile(domain)}),
          flush=True)
    """)

_WRITER = textwrap.dedent("""\
    import json, os, sys
    from pathlib import Path
    from ptest import contracts as C, platform, scheduler

    os.setsid()  # the namespace init has pgid 0 until it leads a session
    spec = json.loads(sys.argv[1])
    domain = C.DomainPaths(root=Path(spec["root"]),
                           machine_config=Path(spec["machine_config"]),
                           ledger=Path(spec["ledger"]), marker=Path(spec["marker"]),
                           fixture=spec["fixture"], domain_id=spec["domain_id"])
    owner = platform.process_identity(os.getpid())
    checkout = C.CheckoutIdentity(project_id="3" * 32, checkout_id="4" * 32,
                                  root=Path(spec["root"]) / "observer-writes")
    ticket = scheduler.enqueue(domain, C.AdmissionRequest(
        run_id=spec["run_id"], checkout=checkout, owner=owner, slots=1,
        exclusive=False, fixture=spec["fixture"]))
    print(json.dumps({"state": scheduler.poll(domain, ticket).state.value}),
          flush=True)
    """)


def _probe_unshare() -> bool:
    if shutil.which("unshare") is None:
        return False
    probe = subprocess.run(
        ["unshare", "--user", f"--map-user={os.getuid()}", "--pid", "--fork",
         "--mount-proc", "--kill-child", "true"],
        capture_output=True, timeout=20)
    return probe.returncode == 0


@pytest.fixture(scope="module")
def unshare_available():
    return _probe_unshare()


def _needs_unshare(unshare_available):
    if not unshare_available:
        pytest.skip("unprivileged PID namespaces unavailable")


def _spec(domain):
    return json.dumps({"root": str(domain.root), "machine_config": str(domain.machine_config),
                       "ledger": str(domain.ledger), "marker": str(domain.marker),
                       "fixture": domain.fixture, "domain_id": domain.domain_id})


def _read_json_line(proc, what):
    ready, _, _ = select.select([proc.stdout], [], [], _WATCHDOG_S)
    assert ready, f"{what} produced no status line in time"
    line = proc.stdout.readline()
    assert line, f"{what} closed its status pipe"
    return json.loads(line)


def _spawn_foreign_run(domain):
    proc = subprocess.Popen(
        ["unshare", "--user", f"--map-user={os.getuid()}", "--pid", "--fork",
         "--mount-proc", "--kill-child", sys.executable, "-c", _CHILD, _spec(domain)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True)
    recorded = _read_json_line(proc, "foreign recorder")
    return proc, recorded


def _kill_namespace(proc) -> None:
    """SIGKILL the namespace init; the kernel tears the namespace down."""
    try:
        children = psutil.Process(proc.pid).children()
    except (psutil.NoSuchProcess, psutil.Error):
        children = []
    for child in children:
        try:
            os.kill(child.pid, signal.SIGKILL)
        except OSError:
            pass
    proc.wait(timeout=_WATCHDOG_S)
    for stream in (proc.stdout, proc.stderr):
        stream.close()


def _view(domain, run_id):
    return {item.run_id: item for item in scheduler.reconcile(domain)}[run_id]


# -- real namespaces ---------------------------------------------------------------

def test_dead_foreign_namespace_lease_is_released_with_foreign_evidence(case, unshare_available):
    _needs_unshare(unshare_available)
    domain = case.domain(slots=2, jobs=2)
    proc, recorded = _spawn_foreign_run(domain)
    try:
        # The recorded pids are sandbox-local: pid 1 is the namespace init.
        assert recorded["owner_pid"] == 1
        _kill_namespace(proc)
    finally:
        if proc.poll() is None:
            _kill_namespace(proc)
    view = _view(domain, "a" * 32)
    assert view.state in (C.LeaseState.RELEASED, C.LeaseState.CANCELLED), (
        f"dead foreign-namespace lease still holds its slot: state={view.state} "
        f"reasons={view.reasons} recorded={recorded}")
    # Reconcile is read-only: persist the verdict through a write path,
    # which also proves the dead run's slot is free for a follower.
    follower = C.AdmissionRequest(
        run_id="f" * 32, fixture=domain.fixture,
        owner=platform.process_identity(os.getpid()), slots=1, exclusive=False,
        checkout=C.CheckoutIdentity(project_id="7" * 32, checkout_id="8" * 32,
                                    root=domain.root / "follower"))
    assert scheduler.poll(domain, scheduler.enqueue(domain, follower)).grant is not None
    with contextlib.closing(sqlite3.connect(domain.ledger)) as connection:
        message = connection.execute(
            "SELECT reason_message FROM jobs WHERE run_id=?", ("a" * 32,)).fetchone()[0]
    assert message == scheduler.FOREIGN_RELEASE_MESSAGE


def test_sandboxed_observer_never_releases_a_live_host_lease(case, unshare_available):
    from ptest import platform as _platform

    _needs_unshare(unshare_available)
    domain = case.domain(slots=2, jobs=2)
    owner = _platform.process_identity(os.getpid())
    request = C.AdmissionRequest(
        run_id="b" * 32, fixture=domain.fixture, owner=owner, slots=1, exclusive=False,
        checkout=C.CheckoutIdentity(project_id="1" * 32, checkout_id="3" * 32,
                                    root=domain.root / "host"))
    ticket = scheduler.enqueue(domain, request)
    grant = scheduler.poll(domain, ticket).grant
    guard_proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        guard = _platform.process_identity(guard_proc.pid)
        assert scheduler.register_guard(domain, grant, guard)
        # Read-only path: a sandboxed observer reconciles.
        seen = subprocess.run(
            ["unshare", "--user", f"--map-user={os.getuid()}", "--pid", "--fork",
             "--mount-proc", "--kill-child", sys.executable, "-c", _OBSERVER, _spec(domain)],
            capture_output=True, text=True, timeout=60)
        assert seen.returncode == 0, seen.stderr
        # Owner and guard are alive on the host: a foreign observer's /proc
        # cannot see them, and "cannot see" must never become "gone".
        assert json.loads(seen.stdout)["b" * 32] in {"RUNNING", "UNCERTAIN"}, seen.stdout
        # Write path: the sandboxed observer admits another run (which runs
        # recovery over every row, including the live host lease).
        writer = subprocess.run(
            ["unshare", "--user", f"--map-user={os.getuid()}", "--pid", "--fork",
             "--mount-proc", "--kill-child", sys.executable, "-c", _WRITER,
             json.dumps({**json.loads(_spec(domain)), "run_id": "c" * 32})],
            capture_output=True, text=True, timeout=60)
        assert writer.returncode == 0, writer.stderr
        assert _view(domain, "b" * 32).state is C.LeaseState.RUNNING
    finally:
        guard_proc.kill()
        guard_proc.wait(timeout=20)


def test_live_foreign_lease_released_only_after_namespace_death(case, unshare_available):
    _needs_unshare(unshare_available)
    domain = case.domain(slots=2, jobs=2)
    proc, _ = _spawn_foreign_run(domain)
    try:
        # While the sandbox owner lives, host recovery must not touch it.
        assert _view(domain, "a" * 32).state in (C.LeaseState.RUNNING, C.LeaseState.UNCERTAIN)
        other = C.AdmissionRequest(
            run_id="d" * 32, fixture=domain.fixture,
            owner=platform.process_identity(os.getpid()), slots=1, exclusive=False,
            checkout=C.CheckoutIdentity(project_id="5" * 32, checkout_id="6" * 32,
                                        root=domain.root / "host-other"))
        assert scheduler.poll(domain, scheduler.enqueue(domain, other)).grant is not None
        assert _view(domain, "a" * 32).state in (C.LeaseState.RUNNING, C.LeaseState.UNCERTAIN)
        _kill_namespace(proc)
    finally:
        if proc.poll() is None:
            _kill_namespace(proc)
    assert _view(domain, "a" * 32).state in (C.LeaseState.RELEASED, C.LeaseState.CANCELLED)


# -- in-process equivalents (no unshare needed) -----------------------------------------

def _admit(case, domain, run_id, with_guard=True):
    owner = platform.process_identity(os.getpid())
    checkout = C.CheckoutIdentity(project_id="1" * 32, checkout_id="2" * 32,
                                  root=domain.root / run_id[:8])
    ticket = scheduler.enqueue(domain, C.AdmissionRequest(
        run_id=run_id, checkout=checkout, owner=owner, slots=1,
        exclusive=False, fixture=domain.fixture))
    grant = scheduler.poll(domain, ticket).grant
    assert grant is not None
    if with_guard:
        guard_proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
        guard = platform.process_identity(guard_proc.pid)
        assert scheduler.register_guard(domain, grant, guard)
        return ticket, guard_proc
    return ticket, None


def test_in_process_dead_foreign_lease_releases_on_free_lock(case, monkeypatch):
    domain = case.domain(slots=2, jobs=2)
    run_id = "e" * 32
    monkeypatch.setattr(leases, "namespace_identity", lambda: "pid:[11]@11")
    ticket, guard_proc = _admit(case, domain, run_id)
    try:
        monkeypatch.setattr(leases, "namespace_identity", lambda: "pid:[22]@22")
        # Lock held by this process: a live foreign lease never releases.
        assert _view(domain, run_id).state is C.LeaseState.RUNNING
        guard_proc.kill()
        guard_proc.wait(timeout=20)
        leases.release_held(domain.root, run_id)
        view = _view(domain, run_id)
        assert view.state is C.LeaseState.RELEASED
        assert view.reasons and view.reasons[0].message == scheduler.FOREIGN_RELEASE_MESSAGE
    finally:
        if guard_proc.poll() is None:
            guard_proc.kill()
            guard_proc.wait(timeout=20)


def test_in_process_foreign_invalid_sidecar_never_releases(case, monkeypatch):
    domain = case.domain()
    run_id = "f" * 32
    ticket, _ = _admit(case, domain, run_id, with_guard=False)
    monkeypatch.setattr(leases, "namespace_identity", lambda: "pid:[22]@22")
    with open(domain.root / "leases" / f"{run_id}.json", "wb") as stream:
        stream.write(b"corrupt")
    # An unprovable foreign row is left unchanged: still granted, never
    # released, and a further poll keeps the grant.
    assert _view(domain, run_id).state is C.LeaseState.GRANTED
    assert scheduler.poll(domain, ticket).grant is not None


def test_in_process_foreign_replaced_lock_never_releases(case, monkeypatch):
    from ptest import files as _files

    domain = case.domain()
    run_id = "0" * 32
    ticket, _ = _admit(case, domain, run_id, with_guard=False)
    monkeypatch.setattr(leases, "namespace_identity", lambda: "pid:[22]@22")
    os.unlink(domain.root / "leases" / f"{run_id}.lock")
    replacement = _files.create_locked(domain.root / "leases", f"{run_id}.lock")
    try:
        assert _view(domain, run_id).state is C.LeaseState.GRANTED
    finally:
        os.close(replacement)


def test_in_process_unknown_observer_uses_lock_only(case, monkeypatch):
    domain = case.domain()
    run_id = "1" * 32
    ticket, guard_proc = _admit(case, domain, run_id)
    try:
        leases.release_held(domain.root, run_id)
        monkeypatch.setattr(leases, "namespace_identity", lambda: "unknown")
        guard_proc.kill()
        guard_proc.wait(timeout=20)
        assert _view(domain, run_id).state is C.LeaseState.RELEASED
    finally:
        if guard_proc.poll() is None:
            guard_proc.kill()
            guard_proc.wait(timeout=20)


def test_enqueue_failure_leaves_no_row_and_no_files(case, monkeypatch):
    from ptest import files as _files

    domain = case.domain()
    run_id = "2" * 32
    seen_at_create = {}

    real_create = leases.create

    def recording_create(root, rid):
        with contextlib.closing(sqlite3.connect(domain.ledger)) as connection:
            seen_at_create["rows"] = connection.execute(
                "SELECT count(*) FROM jobs WHERE run_id=?", (rid,)).fetchone()[0]
        return real_create(root, rid)

    real_exclusive = _files.create_exclusive

    def selective_exclusive(root, relative, data, **kwargs):
        if str(relative) == f"{run_id}.json":
            raise C.Problem(code="state-unavailable", message="no space",
                            phase="files", retryable=False)
        return real_exclusive(root, relative, data, **kwargs)

    monkeypatch.setattr(leases, "create", recording_create)
    monkeypatch.setattr(_files, "create_exclusive", selective_exclusive)
    owner = platform.process_identity(os.getpid())
    checkout = C.CheckoutIdentity(project_id="1" * 32, checkout_id="2" * 32,
                                  root=domain.root / "doomed")
    with pytest.raises(C.Problem) as caught:
        scheduler.enqueue(domain, C.AdmissionRequest(
            run_id=run_id, checkout=checkout, owner=owner, slots=1,
            exclusive=False, fixture=domain.fixture))
    assert caught.value.code == "state-unavailable"
    assert caught.value.message == "run lease cannot be created"
    # No committed row existed when the lease was created, and the rollback
    # left neither a row nor any lease file behind.
    assert seen_at_create["rows"] == 0
    with contextlib.closing(sqlite3.connect(domain.ledger)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM jobs WHERE run_id=?", (run_id,)).fetchone()[0] == 0
    assert list((domain.root / "leases").iterdir()) == []


# -- mixed-version safety ------------------------------------------------------------------

def _frozen_schema_snapshot():
    return {
        "tables": {"domain", "jobs", "job_resources", "observations"},
        "columns": {
            "domain": {"singleton", "domain_id", "schema_version", "protocol_version",
                       "ledger_device", "ledger_inode", "marker_device", "marker_inode",
                       "boot_id", "config_slots", "config_jobs", "config_memory",
                       "config_generation"},
            "jobs": {"run_id", "sequence", "state", "checkout_id", "requested_slots",
                     "slots", "exclusive", "memory_estimate", "reserved_memory",
                     "enqueue_time", "grant_time", "deadline", "phase", "fixture",
                     "owner_pid", "owner_birth", "owner_uid", "owner_pgid", "guard_pid",
                     "guard_birth", "guard_uid", "guard_pgid", "nonce", "generation",
                     "reason_code", "reason_message", "final_status", "final_exit_code",
                     "final_committed", "final_source_valid"},
            "job_resources": {"run_id", "resource"},
            "observations": {"run_id", "pid", "birth", "uid", "pgid", "uncertain"},
        },
        "objects": {("table", "domain"), ("table", "jobs"), ("table", "job_resources"),
                    ("table", "observations"),
                    ("index", "sqlite_autoindex_jobs_1"), ("index", "sqlite_autoindex_jobs_2"),
                    ("index", "sqlite_autoindex_job_resources_1"),
                    ("index", "sqlite_autoindex_observations_1")},
        "user_version": 1,
        "marker_keys": {"domain_id", "ledger_device", "ledger_inode",
                        "protocol_version", "schema_version"},
    }


def _live_snapshot(domain):
    with contextlib.closing(sqlite3.connect(domain.ledger)) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        columns = {table: {item[1] for item in connection.execute(f"PRAGMA table_info({table})")}
                   for table in sorted(tables)}
        objects = {(row[0], row[1]) for row in connection.execute(
            "SELECT type, name FROM sqlite_master")}
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    marker = json.loads((domain.root / "domain.json").read_bytes())
    return {"tables": tables, "columns": columns, "objects": objects,
            "user_version": user_version, "marker_keys": set(marker)}


def test_mixed_version_schema_stays_frozen(case, monkeypatch):
    domain = case.domain(slots=2, jobs=2)
    run_id = "3" * 32
    owner = platform.process_identity(os.getpid())
    checkout = C.CheckoutIdentity(project_id="1" * 32, checkout_id="2" * 32,
                                  root=domain.root / "mixed")
    ticket = scheduler.enqueue(domain, C.AdmissionRequest(
        run_id=run_id, checkout=checkout, owner=owner, slots=1,
        exclusive=False, fixture=domain.fixture))
    grant = scheduler.poll(domain, ticket).grant
    guard_proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        guard = platform.process_identity(guard_proc.pid)
        assert scheduler.register_guard(domain, grant, guard)
        guard_proc.kill()
        guard_proc.wait(timeout=20)
        # The test process itself is the recorded owner and stays alive:
        # fake only its absence (the guard is really dead, the lock is
        # really freed) so the release exercises the real write path.
        real_observe = scheduler._observe_process
        monkeypatch.setattr(
            scheduler, "_observe_process",
            lambda pid: ((None, True) if pid == owner.pid else real_observe(pid)))
        leases.release_held(domain.root, run_id)
        outcome = scheduler.release_run(domain, run_id)
        assert outcome.released is True
    finally:
        if guard_proc.poll() is None:
            guard_proc.kill()
            guard_proc.wait(timeout=20)
    assert _live_snapshot(domain) == _frozen_schema_snapshot()


def test_previous_release_reconciles_and_enqueues_on_this_domain(case):
    shown = subprocess.run(["git", "show", "63e4b6f:src/ptest/scheduler.py"],
                           capture_output=True, timeout=30, cwd=Path(__file__).parent)
    if shown.returncode != 0:
        pytest.skip("git history for 63e4b6f is unavailable")
    module_name = "ptest._scheduler_046"
    # Executed from an in-memory "<...>" filename so coverage never tries to
    # report on a file that is gone by report time.
    module = types.ModuleType(module_name)
    module.__package__ = "ptest"
    sys.modules[module_name] = module
    try:
        exec(compile(shown.stdout, "<scheduler-046>", "exec"), module.__dict__)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    try:
        domain = case.domain()
        run_id = "4" * 32
        owner = platform.process_identity(os.getpid())
        checkout = C.CheckoutIdentity(project_id="1" * 32, checkout_id="2" * 32,
                                      root=domain.root / "old")
        ticket = module.enqueue(domain, C.AdmissionRequest(
            run_id=run_id, checkout=checkout, owner=owner, slots=1,
            exclusive=False, fixture=domain.fixture))
        assert module.poll(domain, ticket).grant is not None
        assert {item.run_id for item in module.reconcile(domain)} == {run_id}
    finally:
        sys.modules.pop(module_name, None)
