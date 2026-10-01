"""scheduler.release_run decision table, refusal texts and ceilings.

Kernel-faked process table (the ``world`` fixture below mirrors
test_scheduler.py); real lease files on disk. Covers the ReleaseOutcome
field pin T4's CLI fake relies on, every §5.3 row, each exact refusal
text, force before/after the ceiling, force never overriding a held
lock, CAS loss, final columns, observation cleanup, post-commit file
cleanup, unknown/already-terminal ids, bootstrap-lock release, and the lease
lifecycle through enqueue/poll/finish/cancel (create-before-row, sweep,
fail-closed creation, foreign rows omitted from holders and nesting).
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import sqlite3
from types import SimpleNamespace

import pytest
import psutil

from ptest import contracts as C
from ptest import leases
from ptest import platform
import ptest.scheduler as scheduler
from ptest.scheduler import enqueue, poll, register_guard


@pytest.fixture
def world(monkeypatch):
    owner = platform.process_identity(os.getpid())
    guard = C.ProcessIdentity(pid=900001, birth=1.0, uid=os.getuid(), pgid=900001)
    state = SimpleNamespace(now=100.0, owner=owner, guard=guard,
                            identities={owner.pid: owner, guard.pid: guard},
                            absent=set(), groups={guard.pgid: True},
                            children={})

    def process(pid):
        if pid in state.absent:
            raise psutil.NoSuchProcess(pid)

        def children():
            return [SimpleNamespace(pid=child) for child in state.children.get(pid, ())]

        return SimpleNamespace(pid=pid, ppid=lambda: 0, children=children)

    def kill(pid, signal):
        assert signal == 0
        if pid in state.absent:
            raise ProcessLookupError(errno.ESRCH, "fixture absent")

    monkeypatch.setattr(scheduler, "_now", lambda: state.now)
    monkeypatch.setattr(platform, "process_identity", lambda pid: state.identities.get(pid))
    monkeypatch.setattr(platform, "probe_group", lambda pgid: C.GroupObservation(
        exists=state.groups.get(pgid, False),
        permission=state.groups.get(pgid, False) is not None, checked_at=state.now))
    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(psutil, "Process", process)
    return state


def _request(world, domain, run_id, **kwargs):
    checkout = C.CheckoutIdentity(
        project_id="ab" * 16, checkout_id="cd" * 16, root=domain.root / run_id[:8])
    return C.AdmissionRequest(
        run_id=run_id, checkout=checkout, owner=world.owner, slots=1,
        exclusive=False, fixture=domain.fixture, **kwargs)


def _running(case, domain, world, run_id):
    ticket = enqueue(domain, _request(world, domain, run_id))
    grant = poll(domain, ticket).grant
    assert grant is not None
    assert register_guard(domain, grant, world.guard)
    return ticket, grant


def _gone(world, identity):
    world.identities.pop(identity.pid, None)
    world.absent.add(identity.pid)
    world.groups[identity.pgid] = False


def _sql(domain, query, parameters=()):
    with contextlib.closing(sqlite3.connect(domain.ledger)) as connection:
        return connection.execute(query, parameters).fetchall()


def _refuse(case, domain, run_id, force=False):
    with pytest.raises(C.Problem) as caught:
        scheduler.release_run(domain, run_id, force=force)
    assert caught.value.code == "ownership-uncertain"
    assert caught.value.phase == "scheduler"
    return caught.value.message


# -- contract pins ---------------------------------------------------------------

def test_release_outcome_fields_match_the_frozen_tuple():
    assert scheduler.ReleaseOutcome._fields == (
        "run_id", "released", "previous_state", "state",
        "slots", "checkout_id", "evidence", "forced")


def test_release_force_after_is_setup_plus_compound_plus_hour():
    assert scheduler.RELEASE_FORCE_AFTER_S == 90300.0
    assert scheduler.RELEASE_FORCE_AFTER_S == (
        C.DEFAULT_SETUP_TIMEOUT_S + C.MAX_COMPOUND_TIMEOUT_S + 3600.0)


def test_release_rejects_bad_args(case, world):
    domain = case.domain()
    with pytest.raises(C.Problem) as caught:
        scheduler.release_run(domain, "not-hex")
    assert caught.value.code == "invalid-config"
    with pytest.raises(C.Problem) as caught:
        scheduler.release_run(domain, "a" * 32, force="yes")
    assert caught.value.code == "invalid-config"
    ticket = enqueue(domain, _request(world, domain, "b0" * 16))
    assert poll(domain, ticket).grant is not None
    with pytest.raises(C.Problem) as caught:
        scheduler.release_run(domain, "a" * 32)
    assert caught.value.code == "state-unavailable"
    assert caught.value.message == "no retained lease has this run id"


def test_release_already_terminal_reports_without_releasing(case, world):
    domain = case.domain()
    run_id = "a1" * 16
    ticket, grant = _running(case, domain, world, run_id)
    _gone(world, world.guard)
    _gone(world, world.owner)
    leases.release_held(domain.root, run_id)
    first = scheduler.release_run(domain, run_id)
    assert first.released is True
    second = scheduler.release_run(domain, run_id)
    assert second.released is False
    assert second.previous_state == first.state
    assert second.state == first.state
    assert second.evidence == scheduler.EVIDENCE_ALREADY
    assert second.forced is False
    assert second.slots == 1


def test_release_recovered_in_call_when_recovery_finishes_first(case, world):
    domain = case.domain()
    run_id = "b2" * 16
    ticket = enqueue(domain, _request(world, domain, run_id))
    assert poll(domain, ticket).grant is not None
    # Owner vanishes before any guard registers: the release transaction's
    # own reconcile cancels the row, and release reports the recovery.
    # previous_state is GRANTED: poll already granted the ticket above.
    _gone(world, world.owner)
    outcome = scheduler.release_run(domain, run_id)
    assert outcome.released is True
    assert outcome.previous_state == "GRANTED"
    assert outcome.state == "CANCELLED"
    assert outcome.evidence == scheduler.EVIDENCE_RECOVERED
    assert outcome.forced is False


# -- same-namespace rows ------------------------------------------------------------

def _delete_lock(domain, run_id):
    """Remove the lock file out of band: the probe goes INDETERMINATE.

    A freed lock would substitute for process liveness in release
    evidence, so liveness refusals need a missing lock instead.
    """
    os.unlink(domain.root / "leases" / f"{run_id}.lock")
    leases.release_held(domain.root, run_id)


def test_release_same_live_owner_refuses(case, world):
    domain = case.domain()
    run_id = "c3" * 16
    _running(case, domain, world, run_id)
    _delete_lock(domain, run_id)
    message = _refuse(case, domain, run_id)
    assert message == f"owner pid {world.owner.pid} is still running"


def test_release_same_live_owner_refuses_even_when_the_lock_is_free(case, world):
    domain = case.domain()
    run_id = "c4" * 16
    _running(case, domain, world, run_id)
    leases.release_held(domain.root, run_id)  # the lock is now acquirable
    sidecar = leases.read_sidecar(domain.root, run_id)
    assert leases.probe(domain.root, run_id, sidecar) == leases.ACQUIRABLE
    message = _refuse(case, domain, run_id)
    assert message == f"owner pid {world.owner.pid} is still running"
    assert _sql(domain, "SELECT state FROM jobs") != [("RELEASED",)]


def test_release_same_live_guard_refuses_even_when_the_lock_is_free(case, world):
    domain = case.domain()
    run_id = "c5" * 16
    _running(case, domain, world, run_id)
    leases.release_held(domain.root, run_id)
    _gone(world, world.owner)
    world.groups[world.guard.pgid] = True
    world.identities[world.guard.pid] = world.guard
    world.absent.discard(world.guard.pid)
    message = _refuse(case, domain, run_id)
    assert message in (f"guard pid {world.guard.pid} is still running",
                       f"process group {world.guard.pgid} still has running members")


def test_release_same_indeterminate_owner_refuses_without_claim(case, world):
    domain = case.domain()
    run_id = "d4" * 16
    _running(case, domain, world, run_id)
    _delete_lock(domain, run_id)
    world.identities.pop(world.owner.pid)  # neither live nor absent
    message = _refuse(case, domain, run_id)
    assert message == f"owner pid {world.owner.pid} cannot be checked"


def test_release_same_live_guard_refuses(case, world):
    domain = case.domain()
    run_id = "e5" * 16
    _running(case, domain, world, run_id)
    _delete_lock(domain, run_id)
    _gone(world, world.owner)
    message = _refuse(case, domain, run_id)
    assert message == f"guard pid {world.guard.pid} is still running"


def test_release_same_live_group_refuses(case, world):
    domain = case.domain()
    run_id = "f6" * 16
    _running(case, domain, world, run_id)
    leases.release_held(domain.root, run_id)
    _gone(world, world.owner)
    world.identities.pop(world.guard.pid, None)
    world.absent.add(world.guard.pid)  # guard dead, group still reported live
    message = _refuse(case, domain, run_id)
    assert message == f"process group {world.guard.pgid} still has running members"


def test_release_same_unchecked_group_refuses(case, world):
    domain = case.domain()
    run_id = "07" * 16
    _running(case, domain, world, run_id)
    leases.release_held(domain.root, run_id)
    _gone(world, world.owner)
    _gone(world, world.guard)
    world.groups[world.guard.pgid] = None
    message = _refuse(case, domain, run_id)
    assert message == f"process group {world.guard.pgid} cannot be checked"


def test_release_same_dead_processes_are_recovered_not_reproven(case, world):
    # Normal recovery finishes a provably dead same-namespace row inside
    # the release transaction, so release reports the recovery instead of
    # re-proving it with its own evidence path.
    domain = case.domain()
    run_id = "18" * 16
    _running(case, domain, world, run_id)
    leases.release_held(domain.root, run_id)  # owner gone: lock is free
    _gone(world, world.owner)
    _gone(world, world.guard)
    outcome = scheduler.release_run(domain, run_id)
    assert outcome.released is True
    assert outcome.previous_state == "RUNNING"
    assert outcome.state == "RELEASED"
    assert outcome.evidence == scheduler.EVIDENCE_RECOVERED
    assert outcome.forced is False
    assert outcome.checkout_id == "cd" * 16
    assert outcome.slots == 1
    # Recovery's own terminal write carries no final_* columns (it is a
    # verdict, not an operator release with a committed result).
    row = _sql(domain, "SELECT state, phase, final_status, final_exit_code, "
                       "reason_code FROM jobs")
    assert row == [("RELEASED", "complete", None, None, "ownership-uncertain")]
    assert _sql(domain, "SELECT * FROM observations") == []
    # Post-commit file cleanup: lock and sidecar are gone, fd dropped.
    assert list((domain.root / "leases").iterdir()) == []
    assert leases.held_fd(domain.root, run_id) is None


def test_release_same_dead_processes_without_lock_are_recovered(case, world):
    domain = case.domain()
    run_id = "29" * 16
    _running(case, domain, world, run_id)
    _gone(world, world.owner)
    _gone(world, world.guard)
    # Lock file deleted out of band: no lock proof is needed because the
    # process table here proves the same-namespace row dead first.
    _delete_lock(domain, run_id)
    outcome = scheduler.release_run(domain, run_id)
    assert outcome.released is True
    assert outcome.evidence == scheduler.EVIDENCE_RECOVERED


def test_release_same_unprovable_owner_uses_lock_evidence(case, world):
    # Recovery cannot finish the row (the owner is neither live nor
    # absent), so release proves it with the free lock instead.
    domain = case.domain()
    run_id = "39" * 16
    _running(case, domain, world, run_id)
    leases.release_held(domain.root, run_id)
    world.identities.pop(world.owner.pid)
    _gone(world, world.guard)
    outcome = scheduler.release_run(domain, run_id)
    assert outcome.released is True
    assert outcome.previous_state == "RUNNING"
    assert outcome.state == "RELEASED"
    assert outcome.evidence == scheduler.EVIDENCE_LOCK
    assert outcome.forced is False


def test_release_evidence_helpers_report_process_table_evidence(case, world):
    # White-box: the helper paths normal recovery usually reaches first.
    row = {"owner_pid": world.owner.pid, "owner_birth": world.owner.birth,
           "owner_uid": world.owner.uid, "owner_pgid": world.owner.pgid,
           "guard_pid": world.guard.pid, "guard_birth": world.guard.birth,
           "guard_uid": world.guard.uid, "guard_pgid": world.guard.pgid}
    _gone(world, world.owner)
    _gone(world, world.guard)
    assert scheduler._release_evidence_same(dict(row), leases.INDETERMINATE) == \
        scheduler.EVIDENCE_PROCESS
    assert scheduler._release_evidence_legacy(dict(row)) == scheduler.EVIDENCE_LEGACY
    assert scheduler._release_evidence_same(dict(row), leases.ACQUIRABLE) == \
        scheduler.EVIDENCE_LOCK


# -- foreign rows ---------------------------------------------------------------------

def test_release_foreign_live_lease_is_never_released(case, world, monkeypatch):
    domain = case.domain()
    run_id = "3a" * 16
    _running(case, domain, world, run_id)
    monkeypatch.setattr(leases, "namespace_identity", lambda: "pid:[999]@999")
    # The lock is held by this process: the foreign lease is live.
    message = _refuse(case, domain, run_id)
    assert message == "lease lock is held: its owner or guard is still running"
    views = {item.run_id: item for item in scheduler.reconcile(domain)}
    assert views[run_id].state is C.LeaseState.RUNNING


def test_release_foreign_free_lock_is_recovered_as_incomplete(case, world, monkeypatch):
    # Recovery judges the foreign row by its free lock first, so release
    # reports the recovery; the row keeps the foreign release message.
    domain = case.domain()
    run_id = "4b" * 16
    _running(case, domain, world, run_id)
    leases.release_held(domain.root, run_id)
    monkeypatch.setattr(leases, "namespace_identity", lambda: "pid:[999]@999")
    outcome = scheduler.release_run(domain, run_id)
    assert outcome.released is True
    assert outcome.previous_state == "RUNNING"
    assert outcome.state == "RELEASED"
    assert outcome.evidence == scheduler.EVIDENCE_RECOVERED
    assert outcome.forced is False
    row = _sql(domain, "SELECT reason_message, final_status, final_exit_code FROM jobs")
    assert row == [(scheduler.FOREIGN_RELEASE_MESSAGE, None, None)]


def test_release_foreign_unreadable_lock_refuses(case, world, monkeypatch):
    domain = case.domain()
    run_id = "5c" * 16
    _running(case, domain, world, run_id)
    leases.release_held(domain.root, run_id)
    os.unlink(domain.root / "leases" / f"{run_id}.lock")
    monkeypatch.setattr(leases, "namespace_identity", lambda: "pid:[999]@999")
    message = _refuse(case, domain, run_id)
    assert message == "lease lock cannot be checked (missing, replaced or unreadable)"


# -- legacy rows ------------------------------------------------------------------------

def test_release_legacy_dead_processes_are_recovered(case, world):
    # A pre-lease client left no sidecar: the process table here proves
    # the row dead during recovery, so release reports the recovery.
    domain = case.domain()
    run_id = "6d" * 16
    _running(case, domain, world, run_id)
    os.unlink(domain.root / "leases" / f"{run_id}.json")
    leases.release_held(domain.root, run_id)
    _gone(world, world.owner)
    _gone(world, world.guard)
    outcome = scheduler.release_run(domain, run_id)
    assert outcome.released is True
    assert outcome.evidence == scheduler.EVIDENCE_RECOVERED


def test_release_legacy_unprovable_owner_refuses(case, world):
    # A legacy row has no lock to consult, so an owner that is neither
    # live nor absent is never proof of death: release refuses.
    domain = case.domain()
    run_id = "6e" * 16
    _running(case, domain, world, run_id)
    os.unlink(domain.root / "leases" / f"{run_id}.json")
    leases.release_held(domain.root, run_id)
    world.identities.pop(world.owner.pid)
    _gone(world, world.guard)
    message = _refuse(case, domain, run_id)
    assert message == f"owner pid {world.owner.pid} cannot be checked"


def test_release_legacy_live_owner_refuses(case, world):
    domain = case.domain()
    run_id = "7e" * 16
    _running(case, domain, world, run_id)
    os.unlink(domain.root / "leases" / f"{run_id}.json")
    leases.release_held(domain.root, run_id)
    message = _refuse(case, domain, run_id)
    assert message == f"owner pid {world.owner.pid} is still running"


# -- force -------------------------------------------------------------------------------

def test_release_force_before_ceiling_refuses_with_age(case, world):
    domain = case.domain()
    run_id = "8f" * 16
    _running(case, domain, world, run_id)
    leases.release_held(domain.root, run_id)
    message = _refuse(case, domain, run_id, force=True)
    assert message == ("--force is allowed only 25 hours after the grant; "
                       "this run was granted 0 minutes ago")


def test_release_force_after_ceiling_skips_process_checks(case, world):
    domain = case.domain()
    run_id = "90" * 16
    _running(case, domain, world, run_id)
    leases.release_held(domain.root, run_id)
    # Owner, guard and group all still live: only the age grants the force.
    world.now += scheduler.RELEASE_FORCE_AFTER_S + 60.0
    outcome = scheduler.release_run(domain, run_id, force=True)
    assert outcome.released is True
    assert outcome.evidence == scheduler.EVIDENCE_FORCED
    assert outcome.forced is True


def test_release_force_never_overrides_a_held_lock(case, world):
    domain = case.domain()
    run_id = "a0" * 16
    _running(case, domain, world, run_id)
    world.now += scheduler.RELEASE_FORCE_AFTER_S + 60.0
    message = _refuse(case, domain, run_id, force=True)
    assert message == "lease lock is held: its owner or guard is still running"


def test_release_cas_loss_refuses(case, world, monkeypatch):
    # Recovery leaves the row active (the owner is unprovable), then a
    # concurrent nonce change between the re-read and the release write
    # defeats the CAS update.
    domain = case.domain()
    run_id = "b1" * 16
    _running(case, domain, world, run_id)
    leases.release_held(domain.root, run_id)
    world.identities.pop(world.owner.pid)
    _gone(world, world.guard)
    real_write = scheduler._release_write

    def tampering(conn, row):
        conn.execute("UPDATE jobs SET nonce=? WHERE run_id=?", ("ff" * 32, run_id))
        return real_write(conn, row)

    monkeypatch.setattr(scheduler, "_release_write", tampering)
    message = _refuse(case, domain, run_id)
    assert message == "lease changed during release"


def test_release_holds_no_bootstrap_lock_at_return(case, world):
    domain = case.domain()
    run_id = "c2" * 16
    _running(case, domain, world, run_id)
    leases.release_held(domain.root, run_id)
    _gone(world, world.owner)
    _gone(world, world.guard)
    scheduler.release_run(domain, run_id)
    fd = os.open(domain.root / "bootstrap.lock", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# -- lease lifecycle through the scheduler ---------------------------------------------------

def _final():
    return C.Finalization(outcome_id=None, status=C.Status.PASSED, exit_code=0,
                          source_valid=True, committed=True)


def _lease_files(domain):
    return sorted(path.name for path in (domain.root / "leases").iterdir())


def test_lease_exists_and_is_locked_before_the_row_is_visible(case, world, monkeypatch):
    domain = case.domain()
    run_id = "d1" * 16
    seen = {}
    real_create = leases.create

    def recording(root, rid):
        seen["rows_before"] = len(_sql(domain, "SELECT run_id FROM jobs WHERE run_id=?", (rid,)))
        fd = real_create(root, rid)
        seen["held_after_create"] = leases.held_fd(root, rid) == fd
        return fd

    monkeypatch.setattr(leases, "create", recording)
    _running(case, domain, world, run_id)
    assert seen == {"rows_before": 0, "held_after_create": True}
    sidecar = leases.read_sidecar(domain.root, run_id)
    assert sidecar.status == "valid"
    assert leases.probe(domain.root, run_id, sidecar) == leases.HELD


def test_create_failure_fails_closed_with_no_row_and_no_files(case, world, monkeypatch):
    domain = case.domain()
    run_id = "d2" * 16

    def refusing(root, rid):
        raise C.Problem(code="state-unavailable", message="run lease cannot be created",
                        phase="leases")

    monkeypatch.setattr(leases, "create", refusing)
    with pytest.raises(C.Problem) as caught:
        enqueue(domain, _request(world, domain, run_id))
    assert caught.value.code == "state-unavailable"
    assert _sql(domain, "SELECT run_id FROM jobs") == []
    assert leases.run_ids(domain.root) == ()


def test_finish_closes_and_removes_the_lease_files(case, world, monkeypatch):
    domain = case.domain()
    run_id = "d3" * 16
    _, grant = _running(case, domain, world, run_id)
    assert _lease_files(domain) == [f"{run_id}.json", f"{run_id}.lock"]
    with monkeypatch.context() as guard_context:
        guard_context.setattr(os, "getpid", lambda: world.guard.pid)
        assert scheduler.mark_draining(domain, grant, world.guard)
    _gone(world, world.guard)
    proof = scheduler.begin_finalization(domain, grant)
    scheduler.finish(domain, grant, proof, _final())
    assert _lease_files(domain) == []
    assert leases.held_fd(domain.root, run_id) is None


def test_cancel_pending_removes_the_lease_files(case, world):
    domain = case.domain(slots=1, jobs=1)
    first = enqueue(domain, _request(world, domain, "d4" * 16))
    assert poll(domain, first).grant is not None
    queued_id = "d5" * 16
    queued = enqueue(domain, _request(world, domain, queued_id))
    assert f"{queued_id}.lock" in _lease_files(domain)
    assert scheduler.cancel_pending(domain, queued, world.owner) is True
    assert f"{queued_id}.lock" not in _lease_files(domain)
    assert f"{queued_id}.json" not in _lease_files(domain)


def test_sweep_removes_orphans_and_terminal_rows_but_keeps_live_ones(case, world):
    domain = case.domain()
    live_id, orphan_id = "d6" * 16, "d7" * 16
    _running(case, domain, world, live_id)
    leases.create(domain.root, orphan_id)  # no row at all: an orphan
    leases.release_held(domain.root, orphan_id)
    enqueue(domain, _request(world, domain, "d8" * 16))  # any write path sweeps
    files = _lease_files(domain)
    assert f"{orphan_id}.lock" not in files and f"{orphan_id}.json" not in files
    assert f"{live_id}.lock" in files and f"{live_id}.json" in files


def test_sweep_never_removes_a_lease_someone_still_holds(case, world):
    domain = case.domain()
    orphan_id = "d9" * 16
    fd = leases.create(domain.root, orphan_id)
    keeper = os.dup(fd)  # shares the open file description, hence the lock
    try:
        leases.release_held(domain.root, orphan_id)
        enqueue(domain, _request(world, domain, "da" * 16))
        assert f"{orphan_id}.lock" in _lease_files(domain)
    finally:
        os.close(keeper)
    enqueue(domain, _request(world, domain, "db" * 16))
    assert f"{orphan_id}.lock" not in _lease_files(domain)


def test_enqueue_retries_once_over_a_stale_unheld_orphan(case, world, monkeypatch):
    domain = case.domain()
    run_id = "dc" * 16
    leases.create(domain.root, run_id)
    leases.release_held(domain.root, run_id)
    monkeypatch.setattr(scheduler, "_sweep_leases_locked", lambda conn, root: None)
    ticket = enqueue(domain, _request(world, domain, run_id))
    assert poll(domain, ticket).grant is not None
    assert leases.probe(domain.root, run_id, leases.read_sidecar(domain.root, run_id)) == leases.HELD


def test_enqueue_over_a_held_same_name_lease_fails_closed(case, world, monkeypatch):
    domain = case.domain()
    run_id = "dd" * 16
    fd = leases.create(domain.root, run_id)
    keeper = os.dup(fd)
    try:
        leases.release_held(domain.root, run_id)
        monkeypatch.setattr(scheduler, "_sweep_leases_locked", lambda conn, root: None)
        with pytest.raises(C.Problem) as caught:
            enqueue(domain, _request(world, domain, run_id))
        assert caught.value.code == "already-exists"
        assert _sql(domain, "SELECT run_id FROM jobs") == []
    finally:
        os.close(keeper)


def test_foreign_rows_are_omitted_from_holders_and_nested_checks(case, world, monkeypatch):
    domain = case.domain(slots=2, jobs=2)
    run_id = "de" * 16
    _running(case, domain, world, run_id)
    assert [item.run_id for item in scheduler.queue_holders(domain)] == [run_id]
    monkeypatch.setattr(leases, "namespace_identity", lambda: "pid:[999]@999")
    assert scheduler.queue_holders(domain) == ()
    # The foreign guard pid means nothing here: the ancestry check must not
    # consult the process table for it.
    real = scheduler._observe_process
    monkeypatch.setattr(scheduler, "_observe_process", lambda pid: (
        pytest.fail("foreign row's pid was observed") if pid == world.guard.pid
        else real(pid)))
    assert enqueue(domain, _request(world, domain, "df" * 16)) is not None
