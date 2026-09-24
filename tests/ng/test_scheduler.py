"""Executable contracts for the local, fail-closed admission coordinator."""
from __future__ import annotations

import os
import time
import errno
import json
import multiprocessing
import sqlite3
import warnings
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path

import pytest
import psutil

from ptest import contracts as C
from ptest import platform
import ptest.scheduler as scheduler
from ptest.scheduler import (
    begin_finalization,
    cancel_pending,
    enqueue,
    finish,
    poll,
    reconcile,
    register_guard,
)


@pytest.fixture
def world(monkeypatch):
    """Kernel observations and monotonic time; no elapsed-time race assertions."""
    owner = platform.process_identity(os.getpid())
    guard = C.ProcessIdentity(pid=900001, birth=1.0, uid=os.getuid(), pgid=900001)
    state = SimpleNamespace(now=100.0, owner=owner, guard=guard,
                            identities={owner.pid: owner, guard.pid: guard},
                            absent=set(), groups={guard.pgid: True},
                            children={}, parents={}, inaccessible=set(),
                            vanish_on_children=set(), reuse_on_children={})

    def process(pid):
        if pid in state.inaccessible:
            raise psutil.AccessDenied(pid)
        if pid in state.absent:
            raise psutil.NoSuchProcess(pid)

        def children():
            if pid in state.reuse_on_children:
                state.identities[pid] = state.reuse_on_children.pop(pid)
                state.absent.discard(pid)
                raise psutil.NoSuchProcess(pid)
            if pid in state.vanish_on_children:
                state.vanish_on_children.remove(pid)
                state.identities.pop(pid, None)
                state.absent.add(pid)
                raise psutil.NoSuchProcess(pid)
            return [SimpleNamespace(pid=child_pid)
                    for child_pid in state.children.get(pid, ())]

        return SimpleNamespace(pid=pid, ppid=lambda: state.parents.get(pid, 0),
                               children=children)

    def kill(pid, signal):
        assert signal == 0
        if pid in state.absent:
            raise ProcessLookupError(errno.ESRCH, "fixture absent")
        if pid in state.inaccessible:
            raise PermissionError(errno.EPERM, "fixture inaccessible")

    monkeypatch.setattr(scheduler, "_now", lambda: state.now)
    monkeypatch.setattr(platform, "process_identity", lambda pid: state.identities.get(pid))
    monkeypatch.setattr(platform, "probe_group", lambda pgid: C.GroupObservation(
        exists=state.groups.get(pgid, False),
        permission=state.groups.get(pgid, False) is not None, checked_at=state.now))
    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(psutil, "Process", process)
    return state


def _running(case, domain, world, label="running", **kwargs):
    ticket = enqueue(domain, _request(case, domain, label, **kwargs))
    grant = poll(domain, ticket).grant
    assert grant is not None
    assert register_guard(domain, grant, world.guard)
    return ticket, grant


def _proof(grant, pgid, now, **kwargs):
    return C.QuiescenceProof(run_id=grant.run_id, generation=grant.generation,
                            pgid=pgid, checked_at=now, group_absent=True,
                            escaped_survivors=kwargs.get("escaped", False))


def _final():
    return C.Finalization(outcome_id=None, status=C.Status.PASSED, exit_code=0,
                          source_valid=True, committed=True)


def _gone(world, identity):
    world.identities.pop(identity.pid, None)
    world.absent.add(identity.pid)
    world.groups[identity.pgid] = False


def _sql(domain, query, parameters=()):
    with sqlite3.connect(domain.ledger) as conn:
        return conn.execute(query, parameters).fetchall()


def _configure(domain, slots=2, jobs=2, memory=64):
    domain.machine_config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    domain.machine_config.write_text(f"max_slots = {slots}\nmax_jobs = {jobs}\n" +
                                     ("" if memory is None else f"memory_mb = {memory}\n"))
    domain.machine_config.chmod(0o600)


def test_poller_preserves_live_finalizer_and_checkout_charge(case, world, monkeypatch):
    domain = case.domain()
    ticket, grant = _running(case, domain, world)
    follower = enqueue(domain, _request(case, domain, "follower"))
    with monkeypatch.context() as guard_context:
        assert scheduler.mark_draining(domain, grant, _draining_guard(guard_context, world))
    _gone(world, world.guard)
    proof = begin_finalization(domain, grant)
    assert poll(domain, ticket).state is C.LeaseState.FINALIZING
    assert poll(domain, follower).grant is None
    finish(domain, grant, proof, _final())
    assert poll(domain, ticket).state is C.LeaseState.RELEASED
    assert poll(domain, follower).grant is not None


@pytest.mark.parametrize("exists", [True, None])
def test_caller_quiescence_booleans_cannot_release_live_or_unknown_group(case, world, exists):
    domain = case.domain()
    ticket, grant = _running(case, domain, world)
    world.groups[world.guard.pgid] = exists
    with pytest.raises(C.Problem) as caught:
        finish(domain, grant, _proof(grant, world.guard.pgid, world.now), _final())
    assert caught.value.code == "ownership-uncertain"
    assert poll(domain, ticket).state is C.LeaseState.UNCERTAIN


@pytest.mark.parametrize("field,value", [("nonce", "b" * 64), ("generation", 99),
                                        ("domain_id", "b" * 32), ("slots", 2),
                                        ("memory_estimate_mb", 10), ("reserved_memory_mb", 10)])
def test_forged_grant_never_poisons_a_legitimate_row(case, world, field, value):
    domain = case.domain()
    ticket, grant = _running(case, domain, world)
    before = domain.ledger.read_bytes()
    with pytest.raises(C.Problem):
        finish(domain, replace(grant, **{field: value}),
               _proof(grant, world.guard.pgid, world.now), _final())
    assert domain.ledger.read_bytes() == before
    assert poll(domain, ticket).state is C.LeaseState.RUNNING


@pytest.mark.parametrize("observation", ["none", "eperm", "reuse", "uid", "absent"])
def test_unregistered_grant_releases_only_for_proven_absence(case, world, observation):
    domain = case.domain()
    ticket = enqueue(domain, _request(case, domain, "first"))
    grant = poll(domain, ticket).grant
    follower = enqueue(domain, _request(case, domain, "follower"))
    world.identities.pop(world.owner.pid)
    if observation == "eperm":
        world.inaccessible.add(world.owner.pid)
    elif observation in {"reuse", "uid"}:
        world.identities[world.owner.pid] = replace(world.owner, **(
            {"birth": world.owner.birth + 1} if observation == "reuse" else {"uid": os.getuid() + 1}))
    elif observation == "absent":
        world.absent.add(world.owner.pid)
    result = poll(domain, ticket)
    assert result.state is (C.LeaseState.CANCELLED if observation == "absent" else C.LeaseState.UNCERTAIN)
    assert register_guard(domain, grant, world.guard) is False
    if observation != "absent":
        assert poll(domain, follower).grant is None


def test_new_boot_atomically_cancels_old_work_and_rebases_time(case, world, monkeypatch):
    domain = case.domain()
    monkeypatch.setattr(platform, "boot_identity", lambda: "boot-a")
    ticket, grant = _running(case, domain, world)
    queued = enqueue(domain, _request(case, domain, "queued"))
    inode = domain.ledger.stat().st_ino
    monkeypatch.setattr(platform, "boot_identity", lambda: "boot-b")
    world.now = 2.0
    before = domain.ledger.read_bytes()
    assert all(item.state is C.LeaseState.CANCELLED for item in reconcile(domain))
    assert domain.ledger.read_bytes() == before
    fresh = enqueue(domain, _request(case, domain, "fresh"))
    assert poll(domain, fresh).grant is not None
    for old in (ticket, queued):
        state = poll(domain, old)
        assert state.state is C.LeaseState.CANCELLED
        assert state.grant is None
        assert state.problem.code == "ownership-uncertain"
    assert domain.ledger.stat().st_ino == inode
    assert _sql(domain, "SELECT boot_id FROM domain") == [("boot-b",)]
    assert all(item.age_s == 0 for item in reconcile(domain))
    assert register_guard(domain, grant, world.guard) is False


def test_deadline_behind_blocked_head_expires_and_frees_pending_capacity(case, world, monkeypatch):
    domain = case.domain(slots=2, jobs=2)
    first = enqueue(domain, _request(case, domain, "first", locks=("db",)))
    assert poll(domain, first).grant is not None
    head = enqueue(domain, _request(case, domain, "head", locks=("db",)))
    tail = enqueue(domain, _request(case, domain, "tail", deadline=105))
    monkeypatch.setattr(scheduler, "MAX_PENDING_JOBS", 2, raising=False)
    world.now = 106
    result = poll(domain, tail)
    assert result.state is C.LeaseState.CANCELLED
    assert result.problem.code == "queue-timeout"
    assert poll(domain, head).grant is None
    later = enqueue(domain, _request(case, domain, "later"))
    assert poll(domain, later).position == 1


def test_existing_status_is_read_only_and_reports_current_queue_wait(case, world):
    domain = case.domain()
    ticket = enqueue(domain, _request(case, domain, "first"))
    assert poll(domain, ticket).grant is not None
    tail = enqueue(domain, _request(case, domain, "tail"))
    world.now += 17
    _gone(world, world.owner)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in domain.root.iterdir()}
    views = {item.run_id: item for item in reconcile(domain)}
    assert views[ticket.run_id].state is C.LeaseState.CANCELLED
    assert views[tail.run_id].queue_wait_s == 17
    assert views[tail.run_id].age_s == 17
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in domain.root.iterdir()}


@pytest.mark.parametrize("memory", [128, None])
def test_memory_budget_loosening_waits_for_idle(case, world, monkeypatch, tmp_path, memory):
    domain = _normal_domain(monkeypatch, tmp_path)
    _configure(domain)
    ticket, grant = _running(case, domain, world)
    _configure(domain, memory=memory)
    assert poll(domain, ticket).state is C.LeaseState.RUNNING
    assert _sql(domain, "SELECT config_memory,config_generation FROM domain") == [(64, 0)]
    _gone(world, world.guard)
    finish(domain, grant, _proof(grant, world.guard.pgid, world.now), _final())
    enqueue(domain, _request(case, domain, "after"))
    assert _sql(domain, "SELECT config_memory,config_generation FROM domain") == [(memory, 1)]


def test_nested_active_guard_and_inaccessible_ancestry_fail_closed(case, world):
    domain = case.domain()
    _running(case, domain, world)
    world.parents[world.owner.pid] = world.guard.pid
    with pytest.raises(C.Problem) as caught:
        enqueue(domain, _request(case, domain, "nested"))
    assert caught.value.code == "nested-invocation"
    world.parents.clear()
    world.inaccessible.add(world.owner.pid)
    with pytest.raises(C.Problem) as caught:
        enqueue(domain, _request(case, domain, "unknown"))
    assert caught.value.code == "ownership-uncertain"
    assert _sql(domain, "SELECT count(*) FROM jobs") == [(1,)]


def test_observed_escape_survives_reparenting_and_blocks_forged_release(case, world):
    domain = case.domain()
    ticket, grant = _running(case, domain, world)
    child = C.ProcessIdentity(pid=900002, birth=2.0, uid=os.getuid(), pgid=900002)
    world.identities[child.pid] = child
    world.children[world.guard.pid] = [child.pid]
    assert poll(domain, ticket).state is C.LeaseState.UNCERTAIN
    world.children.clear()
    _gone(world, world.guard)
    with pytest.raises(C.Problem) as caught:
        finish(domain, grant, _proof(grant, world.guard.pgid, world.now), _final())
    assert caught.value.code == "ownership-uncertain"
    view = reconcile(domain)[0]
    assert view.state is C.LeaseState.UNCERTAIN
    assert view.reasons[0].code == "unsupported-detached-descendant"
    world.identities.pop(child.pid)
    assert poll(domain, ticket).state is C.LeaseState.UNCERTAIN
    world.absent.add(child.pid)
    finish(domain, grant, _proof(grant, world.guard.pgid, world.now), _final())
    assert poll(domain, ticket).state is C.LeaseState.RELEASED


def test_more_than_observation_cap_short_lived_descendants_release_all_claims(case, world):
    domain = case.domain(slots=4, jobs=2)
    request = _request(case, domain, "cycling", slots=4, locks=("database",))
    ticket = enqueue(domain, request)
    grant = poll(domain, ticket).grant
    assert grant is not None
    assert register_guard(domain, grant, world.guard)

    for wave in range(5):
        children = [
            C.ProcessIdentity(
                pid=910000 + wave * 64 + offset,
                birth=float(wave * 64 + offset + 2),
                uid=os.getuid(),
                pgid=world.guard.pgid,
            )
            for offset in range(64)
        ]
        world.children[world.guard.pid] = [child.pid for child in children]
        world.identities.update((child.pid, child) for child in children)
        assert poll(domain, ticket).state is C.LeaseState.RUNNING
        world.children.clear()
        for child in children:
            world.identities.pop(child.pid)
            world.absent.add(child.pid)

    follower_request = replace(
        _request(case, domain, "after-cycling", slots=4, locks=("database",)),
        checkout=request.checkout,
    )
    follower = enqueue(domain, follower_request)
    assert poll(domain, follower).state is C.LeaseState.QUEUED
    _gone(world, world.guard)
    finish(domain, grant, _proof(grant, world.guard.pgid, world.now), _final())
    assert poll(domain, ticket).state is C.LeaseState.RELEASED
    assert poll(domain, follower).grant.slots == 4


def test_reused_descendant_pid_deletes_dead_observation_without_escape(case, world):
    domain = case.domain()
    ticket, grant = _running(case, domain, world)
    child = C.ProcessIdentity(pid=900002, birth=2.0, uid=os.getuid(), pgid=world.guard.pgid)
    world.identities[child.pid] = child
    world.children[world.guard.pid] = [child.pid]
    assert poll(domain, ticket).state is C.LeaseState.RUNNING
    assert _sql(domain, "SELECT pid,birth FROM observations") == [(child.pid, child.birth)]

    world.children.clear()
    world.identities[child.pid] = replace(child, birth=3.0, pgid=child.pid)
    assert poll(domain, ticket).state is C.LeaseState.RUNNING
    assert _sql(domain, "SELECT pid,birth FROM observations") == []
    _gone(world, world.guard)
    finish(domain, grant, _proof(grant, world.guard.pgid, world.now), _final())
    assert poll(domain, ticket).state is C.LeaseState.RELEASED


def test_listed_then_reaped_descendant_does_not_leak_lease(case, world):
    domain = case.domain()
    ticket, grant = _running(case, domain, world)
    child = C.ProcessIdentity(pid=900002, birth=2.0, uid=os.getuid(),
                              pgid=world.guard.pgid)
    world.identities[child.pid] = child
    world.children[world.guard.pid] = [child.pid]
    world.vanish_on_children.add(child.pid)

    assert poll(domain, ticket).state is C.LeaseState.RUNNING
    assert _sql(domain, "SELECT pid FROM observations") == []
    assert _sql(domain, "SELECT pid FROM observations WHERE pid=0") == []

    _gone(world, world.guard)
    finish(domain, grant, _proof(grant, world.guard.pgid, world.now), _final())
    assert poll(domain, ticket).state is C.LeaseState.RELEASED


def test_guard_reused_during_children_walk_is_typed_uncertain_and_retains_charge(case, world):
    domain = case.domain(slots=1, jobs=1)
    ticket, _ = _running(case, domain, world)
    follower = enqueue(domain, _request(case, domain, "follower"))
    world.reuse_on_children[world.guard.pid] = replace(
        world.guard, birth=world.guard.birth + 1,
    )

    result = poll(domain, ticket)

    assert result.state is C.LeaseState.UNCERTAIN
    assert result.problem is not None
    assert result.problem.code == "unsupported-detached-descendant"
    waiting = poll(domain, follower)
    assert waiting.state is C.LeaseState.QUEUED
    assert waiting.grant is None


def test_default_queue_survives_two_240_second_predecessors(case, world):
    domain = case.domain()
    first, grant = _running(case, domain, world, "first")
    second = enqueue(domain, _request(case, domain, "second"))
    third = enqueue(domain, _request(case, domain, "third"))
    short = enqueue(domain, _request(case, domain, "short", deadline=400))
    for ticket in (first, second):
        world.now += 240
        _gone(world, world.guard)
        finish(domain, grant, _proof(grant, world.guard.pgid, world.now), _final())
        if ticket == first:
            grant = poll(domain, second).grant
            world.identities[world.guard.pid] = world.guard
            world.absent.remove(world.guard.pid)
            world.groups[world.guard.pgid] = True
            assert register_guard(domain, grant, world.guard)
    assert poll(domain, third).grant is not None
    assert poll(domain, short).problem.code == "queue-timeout"
    assert next(v for v in reconcile(domain) if v.run_id == third.run_id).queue_wait_s == 480


@pytest.mark.parametrize("kind", ["slots", "jobs", "memory", "checkout", "lock", "exclusive"])
def test_atomic_admission_honors_each_independent_resource_bound(case, world, monkeypatch, tmp_path, kind):
    domain = _normal_domain(monkeypatch, tmp_path)
    _configure(domain, slots=2, jobs=1 if kind == "jobs" else 2,
               memory=64 if kind == "memory" else None)
    request = _request(case, domain, "first", slots=2 if kind == "slots" else 1,
                       memory_mb=40, locks=("db",) if kind == "lock" else (),
                       exclusive=kind == "exclusive")
    first = enqueue(domain, request)
    grant = poll(domain, first).grant
    assert grant.memory_estimate_mb == 40 * grant.slots
    other = _request(case, domain, "other", memory_mb=40, locks=request.locks)
    if kind == "checkout":
        other = replace(other, checkout=request.checkout)
    second = enqueue(domain, other)
    assert poll(domain, second).grant is None
    assert _sql(domain, "SELECT count(*) FROM jobs WHERE state='GRANTED'") == [(1,)]


def test_lowering_limits_and_introducing_memory_budget_keep_existing_charge(case, world, monkeypatch, tmp_path):
    domain = _normal_domain(monkeypatch, tmp_path)
    _configure(domain, slots=4, jobs=2, memory=None)
    first = enqueue(domain, _request(case, domain, "first", slots=3))
    assert poll(domain, first).grant.slots == 3
    _configure(domain, slots=2, jobs=1, memory=64)
    second = enqueue(domain, _request(case, domain, "second"))
    assert poll(domain, second).grant is None
    assert _sql(domain, "SELECT config_slots,config_jobs,config_memory FROM domain") == [(2, 1, 64)]
    assert poll(domain, first).grant.slots == 3


@pytest.mark.parametrize(
    "initial,requested,active_kwargs,follower_kwargs,applied",
    [
        ((4, 2, None), (3, 3, None), {"slots": 1}, {"slots": 3}, (3, 2, None)),
        ((2, 2, None), (4, 1, None), {"slots": 1}, {"slots": 1}, (2, 1, None)),
        ((2, 2, 128), (4, 4, 64), {"memory_mb": 40}, {"memory_mb": 40}, (2, 2, 64)),
    ],
)
def test_busy_reload_applies_each_tightening_and_defers_only_loosening(
        case, world, monkeypatch, tmp_path, initial, requested,
        active_kwargs, follower_kwargs, applied):
    domain = _normal_domain(monkeypatch, tmp_path)
    _configure(domain, slots=initial[0], jobs=initial[1], memory=initial[2])
    _, active_grant = _running(case, domain, world, "active", **active_kwargs)
    _configure(domain, slots=requested[0], jobs=requested[1], memory=requested[2])
    follower = enqueue(domain, _request(case, domain, "follower", **follower_kwargs))

    assert poll(domain, follower).state is C.LeaseState.QUEUED
    assert _sql(
        domain,
        "SELECT config_slots,config_jobs,config_memory,config_generation FROM domain",
    ) == [(*applied, 1)]

    _gone(world, world.guard)
    finish(domain, active_grant, _proof(active_grant, world.guard.pgid, world.now), _final())
    follower_grant = poll(domain, follower).grant
    assert follower_grant is not None
    world.identities[world.guard.pid] = world.guard
    world.absent.remove(world.guard.pid)
    world.groups[world.guard.pgid] = True
    assert register_guard(domain, follower_grant, world.guard)
    _gone(world, world.guard)
    finish(domain, follower_grant, _proof(follower_grant, world.guard.pgid, world.now), _final())

    enqueue(domain, _request(case, domain, "idle-refresh"))
    assert _sql(
        domain,
        "SELECT config_slots,config_jobs,config_memory,config_generation FROM domain",
    ) == [(*requested, 2)]


@pytest.mark.parametrize("memory", [None, 64])
def test_effective_limits_are_typed_read_only_and_reflect_deferred_increase(case, world, monkeypatch, tmp_path, memory):
    domain = _normal_domain(monkeypatch, tmp_path)
    assert scheduler.effective_limits(domain) == C.EffectiveLimits()
    assert not domain.root.exists()
    _configure(domain, memory=memory)
    assert scheduler.effective_limits(domain) == C.EffectiveLimits(max_slots=2, max_jobs=2, memory_mb=memory)
    ticket = enqueue(domain, _request(case, domain, "first"))
    assert poll(domain, ticket).grant is not None
    _configure(domain, slots=4, jobs=3, memory=128)
    before = domain.ledger.read_bytes()
    expected_memory = 128 if memory is None else memory
    assert scheduler.effective_limits(domain) == C.EffectiveLimits(
        max_slots=2, max_jobs=2, memory_mb=expected_memory,
    )
    assert domain.ledger.read_bytes() == before


def test_lowered_memory_budget_waits_for_existing_reservation_to_drain(case, world, monkeypatch, tmp_path):
    domain = _normal_domain(monkeypatch, tmp_path)
    _configure(domain, memory=128)
    ticket, grant = _running(case, domain, world, memory_mb=80)
    _configure(domain, memory=64)
    follower = enqueue(domain, _request(case, domain, "follower", memory_mb=10))
    assert poll(domain, ticket).grant.reserved_memory_mb == 80
    assert poll(domain, follower).grant is None
    assert scheduler.effective_limits(domain).memory_mb == 64
    _gone(world, world.guard)
    finish(domain, grant, _proof(grant, world.guard.pgid, world.now), _final())
    assert poll(domain, follower).grant.reserved_memory_mb == 10


def test_unavailable_boot_never_mutates_existing_state(case, world, monkeypatch):
    domain = case.domain()
    ticket = enqueue(domain, _request(case, domain, "first"))
    before = domain.ledger.read_bytes()
    monkeypatch.setattr(platform, "boot_identity", lambda: None)
    with pytest.raises(C.Problem) as caught:
        poll(domain, ticket)
    assert caught.value.code == "state-unavailable"
    assert domain.ledger.read_bytes() == before


@pytest.mark.parametrize("mode", ["marker-replaced", "db-replaced", "corrupt", "oversize",
                                  "symlink", "hardlink", "marker-missing", "db-missing"])
def test_hostile_or_partial_existing_state_never_recreates_ledger(case, world, tmp_path, mode):
    domain = case.domain()
    ticket = enqueue(domain, _request(case, domain, "first"))
    marker = domain.root / "domain.json"
    outside = tmp_path / "untouched"
    outside.write_bytes(b"outside sentinel")
    outside.chmod(0o600)
    if mode in {"marker-replaced", "db-replaced"}:
        target = marker if mode == "marker-replaced" else domain.ledger
        replacement = domain.root / "replacement"
        replacement.write_bytes(target.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(target)
    elif mode == "corrupt":
        domain.ledger.write_bytes(b"corrupt sqlite")
    elif mode == "oversize":
        with domain.ledger.open("r+b") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
    elif mode in {"symlink", "hardlink"}:
        domain.ledger.unlink()
        if mode == "symlink":
            domain.ledger.symlink_to(outside)
        else:
            os.link(outside, domain.ledger)
    elif mode == "marker-missing":
        marker.unlink()
    else:
        domain.ledger.unlink()
    with pytest.raises(C.Problem) as caught:
        poll(domain, ticket)
    assert caught.value.code in {"unsafe-path", "coordinator-corrupt", "capacity-exceeded"}
    assert outside.read_bytes() == b"outside sentinel"
    if mode == "db-missing":
        assert not domain.ledger.exists()


def test_interrupted_empty_initialization_completes_marker_without_recreating(case, world, monkeypatch):
    domain = case.domain()
    create = scheduler.files.create_exclusive

    def interrupted(root, name, content, **kwargs):
        if name == "domain.json":
            raise C.Problem(code="state-unavailable", message="fixture interruption",
                            phase="scheduler", retryable=False)
        return create(root, name, content, **kwargs)

    monkeypatch.setattr(scheduler.files, "create_exclusive", interrupted)
    with pytest.raises(C.Problem):
        enqueue(domain, _request(case, domain, "interrupted"))
    inode = domain.ledger.stat().st_ino
    monkeypatch.setattr(scheduler.files, "create_exclusive", create)
    ticket = enqueue(domain, _request(case, domain, "restart"))
    assert poll(domain, ticket).grant is not None
    assert domain.ledger.stat().st_ino == inode
    assert _sql(domain, "SELECT count(*) FROM jobs") == [(1,)]


def _initializer(domain, request, channel, pause_publish=False, report_lock=False):
    if pause_publish:
        original = scheduler.files.create_exclusive

        def create(root, name, data, **kwargs):
            if name == "domain.json":
                channel.send("publishing")
                assert channel.recv() == "continue"
            return original(root, name, data, **kwargs)

        scheduler.files.create_exclusive = create
    if report_lock:
        original_lock = scheduler._open_bootstrap
        reported = False

        def lock(*args, **kwargs):
            nonlocal reported
            if not reported:
                channel.send("locking")
                reported = True
            return original_lock(*args, **kwargs)

        scheduler._open_bootstrap = lock
    try:
        ticket = enqueue(domain, request)
        channel.send(("admitted", poll(domain, ticket).state.value))
    except C.Problem as error:
        channel.send(("error", error.code))
    finally:
        channel.close()


def _receive(channel):
    assert channel.poll(2), "bounded IPC deadline exceeded"
    return channel.recv()


def _join(process):
    process.join(2)
    if process.is_alive():
        process.terminate()
        process.join(2)
        pytest.fail("fixture process exceeded deadline")
    assert process.exitcode == 0


def test_two_initializers_serialize_partial_marker_publication(case):
    domain = case.domain(slots=1, jobs=1)
    ctx = multiprocessing.get_context("fork")
    one, child_one = ctx.Pipe()
    two, child_two = ctx.Pipe()
    first = ctx.Process(target=_initializer, args=(domain, _request(case, domain, "first"), child_one, True))
    second = ctx.Process(target=_initializer, args=(domain, _request(case, domain, "second"), child_two, False, True))
    first.start()
    events = [_receive(one)]
    second.start()
    try:
        events.append(_receive(two))
    finally:
        one.send("continue")
        _join(first)
        _join(second)
    events += [_receive(one)]
    if events[1] == "locking":
        events += [_receive(two)]
    one.close()
    two.close()
    assert events == ["publishing", "locking", ("admitted", "GRANTED"), ("admitted", "QUEUED")]
    assert _sql(domain, "SELECT count(*) FROM jobs WHERE state='GRANTED'") == [(1,)]


def _group_child(channel):
    os.setsid()
    channel.send(platform.process_identity(os.getpid()))
    assert channel.recv() == "exit"
    channel.close()


@contextmanager
def _live_group():
    ctx = multiprocessing.get_context("fork")
    parent, child = ctx.Pipe()
    proc = ctx.Process(target=_group_child, args=(child,))
    proc.start()
    try:
        yield _receive(parent), parent, proc
    finally:
        if proc.is_alive():
            parent.send("exit")
        _join(proc)
        parent.close()


def _poller(domain, ticket, channel):
    channel.send(poll(domain, ticket).state.value)
    channel.close()


@pytest.mark.parametrize("forged", [False, True])
def test_real_guard_forged_proof_and_multiprocess_finalizer_poller(case, forged):
    domain = case.domain()
    ticket = enqueue(domain, _request(case, domain, "first"))
    grant = poll(domain, ticket).grant
    events = []
    with _live_group() as (guard, control, proc):
        assert register_guard(domain, grant, guard)
        if forged:
            with pytest.raises(C.Problem):
                finish(domain, grant, _proof(grant, guard.pgid, time.monotonic()), _final())
            events.append("live-proof-rejected")
        control.send("exit")
        _join(proc)
        events.append("guard-reaped")
        ctx = multiprocessing.get_context("fork")
        parent, child = ctx.Pipe()
        poller = ctx.Process(target=_poller, args=(domain, ticket, child))
        poller.start()
        events.append(_receive(parent))
        _join(poller)
        parent.close()
        finish(domain, grant, _proof(grant, guard.pgid, time.monotonic()), _final())
        events.append(poll(domain, ticket).state.value)
    assert events == ((["live-proof-rejected"] if forged else []) +
                      ["guard-reaped", "UNCERTAIN" if forged else "RUNNING", "RELEASED"])


def _racing_admission(domain, request, barrier, channel):
    original = scheduler._grant_queued_locked

    def logged(conn, info, now):
        previous = {r[0] for r in conn.execute("SELECT run_id FROM jobs WHERE state='GRANTED'")}
        original(conn, info, now)
        for row in conn.execute("SELECT run_id,slots,reserved_memory FROM jobs WHERE state='GRANTED' ORDER BY sequence"):
            if row[0] not in previous:
                channel.send(("grant", *tuple(row)))

    scheduler._grant_queued_locked = logged
    barrier.wait(2)
    ticket = enqueue(domain, request)
    poll(domain, ticket)
    channel.send(("done", ticket.sequence))
    channel.close()


@pytest.mark.parametrize("kind", ["slots", "jobs", "memory", "lock"])
def test_multiprocess_admission_event_log_respects_bounds(case, monkeypatch, tmp_path, kind):
    domain = _normal_domain(monkeypatch, tmp_path)
    _configure(domain, slots=2, jobs=1 if kind == "jobs" else 2,
               memory=64 if kind == "memory" else None)
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(4)
    processes, channels = [], []
    for index in range(3):
        parent, child = ctx.Pipe()
        request = _request(case, domain, f"race{index}", slots=2 if kind == "slots" else 1,
                           memory_mb=40, locks=("db",) if kind == "lock" else ())
        process = ctx.Process(target=_racing_admission, args=(domain, request, barrier, child))
        process.start()
        processes.append(process)
        channels.append(parent)
    barrier.wait(2)
    events = []
    for process, channel in zip(processes, channels):
        for _ in range(4):
            event = _receive(channel)
            events.append(event)
            if event[0] == "done":
                break
        else:
            pytest.fail("event log exceeded fixture bound")
        _join(process)
        channel.close()
    grants = [e for e in events if e[0] == "grant"]
    assert len(grants) == 1
    assert grants[0][2] == (2 if kind == "slots" else 1)
    assert grants[0][3] == 40 * grants[0][2]
    assert sorted(e[1] for e in events if e[0] == "done") == [1, 2, 3]
    assert _sql(domain, "SELECT count(*) FROM jobs WHERE state='QUEUED'") == [(2,)]


def test_stale_platform_group_observation_cannot_authorize_release(case, world, monkeypatch):
    domain = case.domain()
    _, grant = _running(case, domain, world)
    _gone(world, world.guard)
    proof = _proof(grant, world.guard.pgid, world.now)
    world.now += 10
    monkeypatch.setattr(platform, "probe_group", lambda pgid: C.GroupObservation(
        exists=False, permission=True, checked_at=proof.checked_at))
    with pytest.raises(C.Problem):
        finish(domain, grant, proof, _final())
    assert _sql(domain, "SELECT state FROM jobs") == [("UNCERTAIN",)]


def test_descendant_escaping_during_absence_probe_retains_charge(case, world, monkeypatch):
    domain = case.domain()
    ticket, grant = _running(case, domain, world)
    child = replace(world.guard, pid=900002, birth=2.0)
    world.identities[child.pid] = child
    world.children[world.guard.pid] = [child.pid]
    assert poll(domain, ticket).state is C.LeaseState.RUNNING
    world.children.clear()
    _gone(world, world.guard)

    def escape(pgid):
        world.identities[child.pid] = replace(child, pgid=child.pid)
        return C.GroupObservation(exists=False, permission=True, checked_at=world.now)

    monkeypatch.setattr(platform, "probe_group", escape)
    with pytest.raises(C.Problem):
        finish(domain, grant, _proof(grant, world.guard.pgid, world.now), _final())
    assert _sql(domain, "SELECT state FROM jobs") == [("UNCERTAIN",)]


@pytest.mark.parametrize("quota,cpuset,expected", [("300000 100000", "0-15", 1),
                                                  ("max 100000", "0-3", 2),
                                                  ("max 100000", "0-15", 4)])
def test_persisted_defaults_include_cgroup_ancestor_bounds(case, world, monkeypatch, tmp_path, quota, cpuset, expected):
    domain = _normal_domain(monkeypatch, tmp_path)
    monkeypatch.setattr(scheduler.os, "sched_getaffinity", lambda _: set(range(16)))
    monkeypatch.setattr(scheduler.os, "cpu_count", lambda: 16)
    kernel = {
        "/proc/self/cgroup": "0::/parent/leaf\n",
        "/proc/self/mountinfo": "1 0 0:1 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
        "/sys/fs/cgroup/cpu.max": "max 100000",
        "/sys/fs/cgroup/cpuset.cpus.effective": "0-15",
        "/sys/fs/cgroup/parent/cpu.max": quota,
        "/sys/fs/cgroup/parent/cpuset.cpus.effective": cpuset,
        "/sys/fs/cgroup/parent/leaf/cpu.max": "max 100000",
        "/sys/fs/cgroup/parent/leaf/cpuset.cpus.effective": "0-15",
    }
    monkeypatch.setattr(scheduler, "_read_cpu_file", lambda path: kernel[str(path)], raising=False)
    ticket = enqueue(domain, _request(case, domain, "cpu", slots=8))
    assert poll(domain, ticket).grant.slots == expected
    assert f"max_slots = {expected}\n" in domain.machine_config.read_text()


def test_missing_nonroot_cgroup_controls_do_not_reduce_normal_defaults(
        case, world, monkeypatch, tmp_path):
    domain = _normal_domain(monkeypatch, tmp_path)
    monkeypatch.setattr(scheduler.os, "sched_getaffinity", lambda _: set(range(16)))
    monkeypatch.setattr(scheduler.os, "cpu_count", lambda: 16)
    kernel = {
        "/proc/self/cgroup": "0::/system.slice/session.scope/leaf\n",
        "/proc/self/mountinfo": "1 0 0:1 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
        "/sys/fs/cgroup/cpu.max": "max 100000",
        "/sys/fs/cgroup/cpuset.cpus.effective": "0-15",
    }

    def read_control(path):
        try:
            return kernel[str(path)]
        except KeyError:
            raise FileNotFoundError(path) from None

    monkeypatch.setattr(scheduler, "_read_cpu_file", read_control, raising=False)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ticket = enqueue(domain, _request(case, domain, "cpu", slots=8))
    assert poll(domain, ticket).grant.slots == 4
    assert domain.machine_config.read_text() == "max_slots = 4\nmax_jobs = 2\n"
    assert not [item for item in caught if issubclass(item.category, RuntimeWarning)]


def test_unreadable_cpu_quota_falls_back_to_one_with_diagnostic(monkeypatch):
    monkeypatch.setattr(scheduler.os, "sched_getaffinity", lambda _: set(range(16)))
    monkeypatch.setattr(scheduler.os, "cpu_count", lambda: 16)

    def unavailable(path):
        raise PermissionError("fixture kernel file unavailable")

    monkeypatch.setattr(scheduler, "_read_cpu_file", unavailable, raising=False)
    with pytest.warns(RuntimeWarning, match="CPU"):
        config = scheduler._default_machine_config()
    assert config == b"max_slots = 1\nmax_jobs = 1\n"


@pytest.mark.parametrize("proof_time", [99.0, 101.0])
def test_proof_before_grant_or_from_future_cannot_release(case, world, proof_time):
    domain = case.domain()
    _, grant = _running(case, domain, world)
    _gone(world, world.guard)
    with pytest.raises(C.Problem):
        finish(domain, grant, _proof(grant, world.guard.pgid, proof_time), _final())
    assert _sql(domain, "SELECT state FROM jobs") == [("UNCERTAIN",)]


@pytest.mark.parametrize("owner", ["absent", "none", "reused"])
def test_dead_or_uncertain_owner_never_promotes_pass(case, world, owner):
    domain = case.domain()
    _, grant = _running(case, domain, world)
    _gone(world, world.guard)
    world.identities.pop(world.owner.pid)
    if owner == "absent":
        world.absent.add(world.owner.pid)
    elif owner == "reused":
        world.identities[world.owner.pid] = replace(world.owner, birth=world.owner.birth + 1)
    with pytest.raises(C.Problem):
        finish(domain, grant, _proof(grant, world.guard.pgid, world.now), _final())
    assert _sql(domain, "SELECT final_committed FROM jobs") == [(None,)]
    if owner == "absent":
        assert reconcile(domain)[0].state is C.LeaseState.RELEASED
    else:
        assert reconcile(domain)[0].state is C.LeaseState.UNCERTAIN


def test_unknown_descendant_scan_is_persisted_even_after_guard_exits(case, world):
    domain = case.domain()
    ticket, grant = _running(case, domain, world)
    world.inaccessible.add(world.guard.pid)
    assert poll(domain, ticket).state is C.LeaseState.UNCERTAIN
    world.inaccessible.clear()
    _gone(world, world.guard)
    with pytest.raises(C.Problem):
        finish(domain, grant, _proof(grant, world.guard.pgid, world.now), _final())
    assert _sql(domain, "SELECT uncertain FROM observations WHERE pid=0") == [(1,)]


def test_reported_unknown_escape_is_not_forgotten_by_a_later_proof(case, world):
    domain = case.domain()
    _, grant = _running(case, domain, world)
    _gone(world, world.guard)
    with pytest.raises(C.Problem):
        finish(domain, grant, _proof(grant, world.guard.pgid, world.now, escaped=True), _final())
    with pytest.raises(C.Problem):
        finish(domain, grant, _proof(grant, world.guard.pgid, world.now), _final())
    assert reconcile(domain)[0].reasons[0].code == "unsupported-detached-descendant"


def test_fixture_checkout_symlink_escape_rejected_before_state_creation(case, tmp_path):
    domain = case.domain()
    escape = domain.root / "escape"
    escape.symlink_to(tmp_path, target_is_directory=True)
    request = _request(case, domain, "escape")
    with pytest.raises(C.Problem) as caught:
        enqueue(domain, request)
    assert caught.value.code == "unsafe-path"
    assert not domain.ledger.exists()


def test_normal_domain_cannot_be_redirected_and_ignores_environment(case, world, monkeypatch, tmp_path):
    domain = _normal_domain(monkeypatch, tmp_path)
    sentinel = tmp_path / "legacy-config"
    sentinel.write_bytes(b"not toml; must never be read")
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "PTEST_CONFIG", "PTEST_RUN_ID"):
        monkeypatch.setenv(name, str(sentinel))
    with pytest.raises(C.Problem) as caught:
        enqueue(replace(domain, root=tmp_path), _request(case, domain, "redirected"))
    assert caught.value.code == "unsafe-path"
    ticket = enqueue(domain, _request(case, domain, "canonical"))
    assert poll(domain, ticket).grant is not None
    assert sentinel.read_bytes() == b"not toml; must never be read"


def test_scheduler_has_no_remote_legacy_or_process_launch_dependency():
    import ast

    tree = ast.parse(Path(scheduler.__file__).read_text())
    imports = {alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
               for alias in node.names}
    assert not imports & {"subprocess", "socket", "requests", "httpx", "spot_queue", "spot_controller"}
    assert not any(isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv", "Popen", "system"}
                   for node in ast.walk(tree))


def test_enqueue_expires_all_deadlines_before_pending_limit_check(case, world, monkeypatch):
    domain = case.domain()
    first = enqueue(domain, _request(case, domain, "first"))
    assert poll(domain, first).grant is not None
    enqueue(domain, _request(case, domain, "head"))
    enqueue(domain, _request(case, domain, "expired", deadline=105))
    monkeypatch.setattr(scheduler, "MAX_PENDING_JOBS", 2)
    world.now = 106
    ticket = enqueue(domain, _request(case, domain, "replacement"))
    assert poll(domain, ticket).position == 1
    with pytest.raises(C.Problem) as caught:
        enqueue(domain, _request(case, domain, "overflow"))
    assert caught.value.code == "capacity-exceeded"


@pytest.mark.parametrize("mode", ["corrupt", "oversize", "symlink", "hardlink", "protocol", "wrong-mode"])
def test_hostile_domain_marker_is_rejected_without_outside_writes(case, world, tmp_path, mode):
    domain = case.domain()
    ticket = enqueue(domain, _request(case, domain, "first"))
    before = domain.ledger.read_bytes()
    marker = domain.root / "domain.json"
    outside = tmp_path / "outside"
    outside.write_bytes(marker.read_bytes())
    outside.chmod(0o600)
    saved = outside.read_bytes()
    if mode == "corrupt":
        marker.write_bytes(b"not json")
    elif mode == "oversize":
        marker.write_bytes(b" " * 65537)
    elif mode == "protocol":
        data = json.loads(marker.read_bytes())
        data["protocol_version"] += 1
        marker.write_text(json.dumps(data))
    elif mode == "wrong-mode":
        marker.chmod(0o644)
    else:
        marker.unlink()
        if mode == "symlink":
            marker.symlink_to(outside)
        else:
            os.link(outside, marker)
    with pytest.raises(C.Problem) as caught:
        poll(domain, ticket)
    assert caught.value.code in {"coordinator-corrupt", "unsafe-path", "protocol-mismatch", "capacity-exceeded"}
    assert domain.ledger.read_bytes() == before
    assert outside.read_bytes() == saved


def test_unidentified_empty_ledger_is_never_initialized_over(case, world):
    domain = case.domain()
    domain.ledger.touch(mode=0o600)
    inode = domain.ledger.stat().st_ino
    with pytest.raises(C.Problem):
        enqueue(domain, _request(case, domain, "first"))
    assert domain.ledger.stat().st_ino == inode
    assert domain.ledger.read_bytes() == b""
    assert not (domain.root / "domain.json").exists()


@pytest.mark.parametrize("value", ["true", "2"])
def test_fixture_marker_version_must_be_exact_supported_integer(case, world, value):
    domain = case.domain()
    domain.marker.write_text(domain.marker.read_text().replace("version = 1", f"version = {value}"))
    with pytest.raises(C.Problem) as caught:
        enqueue(domain, _request(case, domain, "first"))
    assert caught.value.code == "invalid-config"
    assert not domain.ledger.exists()


def test_fixture_limits_cannot_exceed_the_miniature_boundary(case, world):
    domain = case.domain(slots=5, jobs=1)
    with pytest.raises(C.Problem) as caught:
        enqueue(domain, _request(case, domain, "first"))
    assert caught.value.code == "invalid-config"
    assert not domain.ledger.exists()


def test_database_replacement_between_validation_and_open_is_rejected(case, world, monkeypatch):
    domain = case.domain()
    ticket = enqueue(domain, _request(case, domain, "first"))
    replacement = domain.root / "replacement"
    replacement.write_bytes(domain.ledger.read_bytes())
    replacement.chmod(0o600)
    original = scheduler.storage.open_database

    def raced(*args, **kwargs):
        replacement.replace(domain.ledger)
        return original(*args, **kwargs)

    monkeypatch.setattr(scheduler.storage, "open_database", raced)
    with pytest.raises(C.Problem) as caught:
        poll(domain, ticket)
    assert caught.value.code == "unsafe-path"


def test_storage_failure_does_not_admit_or_fall_back(case, world, monkeypatch):
    domain = case.domain()
    ticket = enqueue(domain, _request(case, domain, "first"))
    before = domain.ledger.read_bytes()

    def full(*args, **kwargs):
        raise C.Problem(code="capacity-exceeded", message="fixture storage full",
                        phase="storage", retryable=False)

    monkeypatch.setattr(scheduler.storage, "open_database", full)
    with pytest.raises(C.Problem) as caught:
        poll(domain, ticket)
    assert caught.value.code == "capacity-exceeded"
    assert domain.ledger.read_bytes() == before


def test_deadline_is_rechecked_after_waiting_for_the_transaction(case, world, monkeypatch):
    domain = case.domain()
    ticket = enqueue(domain, _request(case, domain, "deadline", deadline=105))
    original = scheduler._begin

    def waited(conn):
        original(conn)
        world.now = 106

    monkeypatch.setattr(scheduler, "_begin", waited)
    result = poll(domain, ticket)
    assert result.state is C.LeaseState.CANCELLED
    assert result.problem.code == "queue-timeout"
    assert result.grant is None


@pytest.mark.parametrize("failure", ["write", "commit"])
def test_transaction_disk_errors_are_typed_and_always_close_connection(case, world, monkeypatch, failure):
    domain = case.domain()
    enqueue(domain, _request(case, domain, "first"))
    before = domain.ledger.read_bytes()
    original = scheduler.storage.open_database
    closed = []

    class FailingConnection:
        def __init__(self, conn):
            object.__setattr__(self, "conn", conn)

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def __setattr__(self, name, value):
            setattr(self.conn, name, value)

        def execute(self, sql, *args):
            if failure == "write" and "INSERT INTO jobs" in sql:
                raise sqlite3.OperationalError("fixture disk full")
            return self.conn.execute(sql, *args)

        def commit(self):
            if failure == "commit":
                raise sqlite3.OperationalError("fixture disk full")
            return self.conn.commit()

        def close(self):
            closed.append(True)
            self.conn.close()

    monkeypatch.setattr(scheduler.storage, "open_database",
                        lambda *args, **kwargs: FailingConnection(original(*args, **kwargs)))
    with pytest.raises(C.Problem) as caught:
        enqueue(domain, _request(case, domain, "failed"))
    assert caught.value.code == "coordinator-unavailable"
    assert closed == [True]
    assert domain.ledger.read_bytes() == before


def _request(case, domain, label: str, *, slots: int = 1,
             exclusive: bool = False, locks: tuple = (),
             memory_mb: int | None = None, deadline: float = 0.0) -> C.AdmissionRequest:
    identity = platform.process_identity(os.getpid())
    assert identity is not None
    checkout = C.CheckoutIdentity(
        project_id=(label.encode().hex() + "0" * 32)[:32],
        checkout_id=(label.encode().hex() + "1" * 32)[:32],
        root=domain.root / label,
    )
    return C.AdmissionRequest(
        run_id=(label.encode().hex() + "a" * 32)[:32],
        checkout=checkout,
        owner=identity,
        slots=slots,
        exclusive=exclusive,
        locks=locks,
        memory_mb=memory_mb,
        deadline=deadline,
        fixture=domain.fixture,
    )


def _normal_domain(monkeypatch, base: Path) -> C.DomainPaths:
    home = base / "account"
    home.mkdir(mode=0o700, parents=True)
    monkeypatch.setattr(
        scheduler.pwd, "getpwuid",
        lambda _uid: SimpleNamespace(pw_dir=str(home)),
    )
    root = home / ".local" / "state" / "ptest" / "coordination"
    return C.DomainPaths(
        root=root,
        machine_config=home / ".config" / "ptest" / "machine.toml",
        ledger=root / "coordinator.sqlite3",
        marker=root / "domain.json",
        fixture=False,
        domain_id=None,
    )
def test_budget_one_cannot_grant_two(case):
    domain = case.domain(slots=1, jobs=1)
    first = enqueue(domain, _request(case, domain, "first"))
    second = enqueue(domain, _request(case, domain, "second"))
    assert poll(domain, first).grant.slots == 1
    waiting = poll(domain, second)
    assert waiting.state is C.LeaseState.QUEUED
    assert waiting.grant is None


def test_zero_memory_is_rejected_before_state_creation(case):
    domain = case.domain(slots=1, jobs=1)
    with pytest.raises(ValueError, match="positive"):
        _request(case, domain, "zero", memory_mb=0)
    assert not domain.ledger.exists()
    assert not (domain.root / "domain.json").exists()


def test_head_of_line_resource_block_does_not_bypass(case):
    domain = case.domain(slots=2, jobs=2)
    first = enqueue(domain, _request(case, domain, "first", locks=("db",)))
    blocked = enqueue(domain, _request(case, domain, "blocked", locks=("db",)))
    later = enqueue(domain, _request(case, domain, "later"))
    assert poll(domain, first).state is C.LeaseState.GRANTED
    assert poll(domain, blocked).state is C.LeaseState.QUEUED
    assert poll(domain, later).state is C.LeaseState.QUEUED


def test_unknown_memory_does_not_serialize_without_a_machine_budget(case):
    domain = case.domain(slots=2, jobs=2)
    first = enqueue(domain, _request(case, domain, "first"))
    second = enqueue(domain, _request(case, domain, "second"))
    assert poll(domain, first).state is C.LeaseState.GRANTED
    assert poll(domain, second).state is C.LeaseState.GRANTED


def test_expired_queue_is_cancelled_with_a_stable_reason(case):
    domain = case.domain(slots=1, jobs=1)
    ticket = enqueue(domain, _request(case, domain, "expired",
                                      deadline=time.monotonic() - 1))
    state = poll(domain, ticket)
    assert state.state is C.LeaseState.CANCELLED
    assert state.problem is not None
    assert state.problem.code == "queue-timeout"


def test_guard_registration_is_a_nonce_bound_compare_and_swap(case):
    domain = case.domain(slots=1, jobs=1)
    ticket = enqueue(domain, _request(case, domain, "guard"))
    state = poll(domain, ticket)
    assert state.grant is not None
    identity = platform.process_identity(os.getpid())
    assert identity is not None
    assert register_guard(domain, state.grant, identity) is True
    assert register_guard(domain, state.grant, identity) is False
    assert poll(domain, ticket).state is C.LeaseState.RUNNING


def _draining_guard(monkeypatch, world):
    """Make the deterministic registered guard the authenticated caller."""
    monkeypatch.setattr(os, "getpid", lambda: world.guard.pid)
    return world.guard


def test_mark_draining_authenticates_the_live_registered_guard_and_retains_claims(
        case, world, monkeypatch):
    domain = case.domain()
    ticket, grant = _running(case, domain, world)
    child = C.ProcessIdentity(pid=900002, birth=2.0, uid=os.getuid(),
                              pgid=world.guard.pgid)
    world.identities[child.pid] = child
    world.children[world.guard.pid] = (child.pid,)
    reconcile(domain)
    before_claims = _sql(
        domain,
        "SELECT checkout_id,slots,memory_estimate,reserved_memory FROM jobs WHERE run_id=?",
        (grant.run_id,),
    )
    before_resources = _sql(domain, "SELECT resource FROM job_resources WHERE run_id=?", (grant.run_id,))
    before_observations = _sql(
        domain, "SELECT pid,birth,uid,pgid,uncertain FROM observations WHERE run_id=?", (grant.run_id,)
    )
    guard = _draining_guard(monkeypatch, world)

    assert scheduler.mark_draining(domain, grant, guard) is True
    assert _sql(domain, "SELECT state FROM jobs WHERE run_id=?", (grant.run_id,)) == [("DRAINING",)]
    assert _sql(
        domain,
        "SELECT checkout_id,slots,memory_estimate,reserved_memory FROM jobs WHERE run_id=?",
        (grant.run_id,),
    ) == before_claims
    assert _sql(domain, "SELECT resource FROM job_resources WHERE run_id=?", (grant.run_id,)) == before_resources
    assert _sql(
        domain, "SELECT pid,birth,uid,pgid,uncertain FROM observations WHERE run_id=?", (grant.run_id,)
    ) == before_observations


@pytest.mark.parametrize(
    "grant_field,value",
    [
        ("run_id", "b" * 32),
        ("nonce", "b" * 64),
        ("generation", 99),
        ("domain_id", "b" * 32),
        ("slots", 2),
        ("memory_estimate_mb", 10),
        ("reserved_memory_mb", 10),
    ],
)
def test_mark_draining_rejects_each_forged_grant_without_mutating_the_ledger(
        case, world, monkeypatch, grant_field, value):
    domain = case.domain()
    _, grant = _running(case, domain, world)
    before = domain.ledger.read_bytes()

    assert scheduler.mark_draining(
        domain, replace(grant, **{grant_field: value}), _draining_guard(monkeypatch, world)
    ) is False
    assert domain.ledger.read_bytes() == before


@pytest.mark.parametrize(
    "guard_field,value",
    [("pid", 900002), ("birth", 2.0), ("uid", 0), ("pgid", 900002)],
)
def test_mark_draining_rejects_each_forged_guard_without_mutating_the_ledger(
        case, world, monkeypatch, guard_field, value):
    domain = case.domain()
    _, grant = _running(case, domain, world)
    before = domain.ledger.read_bytes()
    _draining_guard(monkeypatch, world)

    assert scheduler.mark_draining(
        domain, grant, replace(world.guard, **{guard_field: value})
    ) is False
    assert domain.ledger.read_bytes() == before


@pytest.mark.parametrize("state", ["RUNNING", "CANCELLING"])
def test_mark_draining_rejects_a_second_handoff_without_mutating_the_ledger(
        case, world, monkeypatch, state):
    domain = case.domain()
    _, grant = _running(case, domain, world)
    with sqlite3.connect(domain.ledger) as conn:
        conn.execute("UPDATE jobs SET state=? WHERE run_id=?", (state, grant.run_id))
    guard = _draining_guard(monkeypatch, world)
    assert scheduler.mark_draining(domain, grant, guard) is True
    assert _sql(domain, "SELECT state FROM jobs WHERE run_id=?", (grant.run_id,)) == [("DRAINING",)]
    before = domain.ledger.read_bytes()

    assert scheduler.mark_draining(domain, grant, guard) is False
    assert domain.ledger.read_bytes() == before


@pytest.mark.parametrize("state", ["FINALIZING", "RELEASED", "CANCELLED", "UNCERTAIN"])
def test_mark_draining_rejects_terminal_or_uncertain_state(
        case, world, monkeypatch, state):
    domain = case.domain()
    _, grant = _running(case, domain, world)
    with sqlite3.connect(domain.ledger) as conn:
        conn.execute("UPDATE jobs SET state=? WHERE run_id=?", (state, grant.run_id))
    before = domain.ledger.read_bytes()

    assert scheduler.mark_draining(domain, grant, _draining_guard(monkeypatch, world)) is False
    assert domain.ledger.read_bytes() == before


def test_mark_draining_allows_the_authenticated_registered_guard_to_drain_from_cancelling(
        case, world, monkeypatch):
    domain = case.domain()
    _, grant = _running(case, domain, world)
    with sqlite3.connect(domain.ledger) as conn:
        conn.execute("UPDATE jobs SET state='CANCELLING' WHERE run_id=?", (grant.run_id,))

    assert scheduler.mark_draining(domain, grant, _draining_guard(monkeypatch, world)) is True
    assert _sql(domain, "SELECT state FROM jobs WHERE run_id=?", (grant.run_id,)) == [("DRAINING",)]


@pytest.mark.parametrize("caller", ["another_process", "reused_pid"])
def test_mark_draining_rejects_a_live_caller_that_is_not_the_registered_guard(
        case, world, monkeypatch, caller):
    domain = case.domain()
    _, grant = _running(case, domain, world)
    if caller == "another_process":
        guard = C.ProcessIdentity(pid=900003, birth=3.0, uid=os.getuid(), pgid=900003)
    else:
        guard = replace(world.guard, birth=world.guard.birth + 5)
    world.identities[guard.pid] = guard
    monkeypatch.setattr(os, "getpid", lambda: guard.pid)

    # Every live-caller check passes; only the stored guard can reject the CAS.
    assert guard.pid == os.getpid() == guard.pgid
    assert guard.uid == os.getuid()
    assert platform.process_identity(guard.pid) == guard
    assert guard != world.guard
    assert _sql(
        domain, "SELECT guard_pid,guard_birth,guard_uid,guard_pgid FROM jobs WHERE run_id=?",
        (grant.run_id,),
    ) == [(world.guard.pid, world.guard.birth, world.guard.uid, world.guard.pgid)]
    before = domain.ledger.read_bytes()

    assert scheduler.mark_draining(domain, grant, guard) is False
    assert domain.ledger.read_bytes() == before


@pytest.mark.parametrize("guard_field", ["pid", "birth", "uid", "pgid"])
def test_mark_draining_rejects_each_stored_guard_mismatch_without_mutating_the_ledger(
        case, world, monkeypatch, guard_field):
    domain = case.domain()
    _, grant = _running(case, domain, world)
    guard = _draining_guard(monkeypatch, world)
    # Isolate each stored column so dropping any one predicate is observable.
    with sqlite3.connect(domain.ledger) as conn:
        conn.execute(
            f"UPDATE jobs SET guard_{guard_field}=? WHERE run_id=?",
            (getattr(guard, guard_field) + 1, grant.run_id),
        )
    assert guard.pid == os.getpid() == guard.pgid
    assert guard.uid == os.getuid()
    assert platform.process_identity(guard.pid) == guard
    before = domain.ledger.read_bytes()

    assert scheduler.mark_draining(domain, grant, guard) is False
    assert domain.ledger.read_bytes() == before


def test_mark_draining_rejects_mismatched_live_caller_identity_and_boot_without_repair(
        case, world, monkeypatch):
    domain = case.domain()
    _, grant = _running(case, domain, world)
    before = domain.ledger.read_bytes()
    monkeypatch.setattr(os, "getpid", lambda: world.guard.pid + 1)
    assert scheduler.mark_draining(domain, grant, world.guard) is False
    assert domain.ledger.read_bytes() == before

    monkeypatch.setattr(os, "getpid", lambda: world.guard.pid)
    world.identities[world.guard.pid] = replace(world.guard, birth=world.guard.birth + 1)
    assert scheduler.mark_draining(domain, grant, world.guard) is False
    assert domain.ledger.read_bytes() == before

    world.identities[world.guard.pid] = world.guard
    monkeypatch.setattr(platform, "boot_identity", lambda: "different-boot")
    assert scheduler.mark_draining(domain, grant, world.guard) is False
    assert domain.ledger.read_bytes() == before


def test_mark_draining_fails_closed_for_an_unobservable_guard_and_never_reconciles_or_repairs(
        case, world, monkeypatch):
    domain = case.domain()
    _, grant = _running(case, domain, world)
    guard = _draining_guard(monkeypatch, world)
    before = domain.ledger.read_bytes()
    # platform.process_identity deliberately maps both absent and inaccessible
    # kernel observations to None; the scheduler must convert that ambiguity
    # to its typed fail-closed result.
    monkeypatch.setattr(platform, "process_identity", lambda _pid: None)
    with pytest.raises(C.Problem) as caught:
        scheduler.mark_draining(domain, grant, guard)
    assert caught.value.code == "ownership-uncertain"
    assert domain.ledger.read_bytes() == before

    monkeypatch.setattr(platform, "process_identity", lambda pid: world.identities.get(pid))
    monkeypatch.setattr(scheduler, "_reconcile_locked", lambda *_args: pytest.fail("must not reconcile"))
    monkeypatch.setattr(scheduler, "_recover_boot_locked", lambda *_args: pytest.fail("must not repair boot"))
    assert scheduler.mark_draining(domain, grant, guard) is True


@pytest.mark.parametrize("failure", ["write", "commit"])
def test_mark_draining_surfaces_transaction_failure_without_success(case, world, monkeypatch, failure):
    domain = case.domain()
    _, grant = _running(case, domain, world)
    guard = _draining_guard(monkeypatch, world)
    before = domain.ledger.read_bytes()
    original = scheduler.storage.open_database

    class FailingConnection:
        def __init__(self, conn):
            object.__setattr__(self, "conn", conn)

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def __setattr__(self, name, value):
            setattr(self.conn, name, value)

        def execute(self, sql, *args):
            if failure == "write" and "UPDATE jobs SET state='DRAINING'" in sql:
                raise sqlite3.OperationalError("fixture disk full")
            return self.conn.execute(sql, *args)

        def commit(self):
            if failure == "commit":
                raise sqlite3.OperationalError("fixture disk full")
            return self.conn.commit()

    monkeypatch.setattr(scheduler.storage, "open_database",
                        lambda *args, **kwargs: FailingConnection(original(*args, **kwargs)))
    with pytest.raises(C.Problem) as caught:
        scheduler.mark_draining(domain, grant, guard)
    assert caught.value.code == "coordinator-unavailable"
    assert domain.ledger.read_bytes() == before


def test_finish_releases_only_with_a_complete_quiescence_proof(case):
    domain = case.domain(slots=1, jobs=1)
    ticket = enqueue(domain, _request(case, domain, "finish"))
    state = poll(domain, ticket)
    assert state.grant is not None
    identity = platform.process_identity(os.getpid())
    assert identity is not None
    assert register_guard(domain, state.grant, identity)
    with pytest.raises(C.Problem, match="finalization proof"):
        finish(
            domain, state.grant,
            C.QuiescenceProof(
                run_id=state.grant.run_id, generation=state.grant.generation,
                pgid=identity.pgid, checked_at=time.monotonic(),
                group_absent=False, escaped_survivors=False,
            ),
            C.Finalization(
                outcome_id=None, status=C.Status.PASSED, exit_code=0,
                source_valid=True, committed=True,
            ),
        )
    view = next(item for item in reconcile(domain) if item.run_id == state.grant.run_id)
    assert view.ownership == "uncertain"


def test_first_normal_admission_creates_only_canonical_private_state(case, monkeypatch, tmp_path):
    domain = _normal_domain(monkeypatch, tmp_path)
    shared = Path(domain.root).parents[2]
    shared.mkdir(mode=0o755)
    legacy = shared / "legacy-sibling"
    legacy.write_bytes(b"must remain untouched")
    ticket = enqueue(domain, _request(case, domain, "normal"))
    assert poll(domain, ticket).state is C.LeaseState.GRANTED
    assert domain.root.is_dir()
    assert domain.ledger.stat().st_mode & 0o777 == 0o600
    assert domain.marker.stat().st_mode & 0o777 == 0o600
    assert domain.machine_config.stat().st_mode & 0o777 == 0o600
    assert legacy.read_bytes() == b"must remain untouched"


def test_reconcile_on_an_absent_normal_domain_is_non_mutating(monkeypatch, tmp_path):
    domain = _normal_domain(monkeypatch, tmp_path)
    assert reconcile(domain) == ()
    assert not domain.root.exists()
    assert not domain.machine_config.exists()


def test_configured_unknown_memory_reserves_the_declared_budget(case, monkeypatch, tmp_path):
    domain = _normal_domain(monkeypatch, tmp_path)
    domain.machine_config.parent.mkdir(mode=0o700, parents=True)
    domain.machine_config.write_text(
        "max_slots = 2\nmax_jobs = 2\nmemory_mb = 64\n", encoding="ascii"
    )
    os.chmod(domain.machine_config, 0o600)
    first = enqueue(domain, _request(case, domain, "first"))
    second = enqueue(domain, _request(case, domain, "second"))
    first_state = poll(domain, first)
    second_state = poll(domain, second)
    assert first_state.grant is not None
    assert first_state.grant.slots == 1
    assert first_state.grant.memory_estimate_mb is None
    assert first_state.grant.reserved_memory_mb == 64
    assert second_state.state is C.LeaseState.QUEUED


def test_cancel_pending_requires_exact_owner_and_ticket_sequence(case, world):
    domain = case.domain(slots=1, jobs=1)
    _running(case, domain, world, "blocker")
    ticket = enqueue(domain, _request(case, domain, "queued"))
    wrong_owner = replace(world.owner, birth=world.owner.birth + 1)

    assert cancel_pending(domain, ticket, wrong_owner) is False
    assert cancel_pending(domain, replace(ticket, sequence=ticket.sequence + 1), world.owner) is False
    assert poll(domain, ticket).state is C.LeaseState.QUEUED


def test_cancel_pending_cancels_only_the_authenticated_queued_ticket(case, world):
    domain = case.domain(slots=1, jobs=1)
    first = enqueue(domain, _request(case, domain, "first"))
    second = enqueue(domain, _request(case, domain, "second"))

    assert cancel_pending(domain, first, world.owner) is True
    assert poll(domain, first).state is C.LeaseState.CANCELLED
    assert poll(domain, second).grant is not None


def test_cancel_pending_cancels_an_unregistered_grant_and_releases_claims(case, world):
    domain = case.domain(slots=1, jobs=1)
    first = enqueue(domain, _request(case, domain, "first"))
    grant = poll(domain, first).grant
    follower = enqueue(domain, _request(case, domain, "follower"))
    assert grant is not None

    assert cancel_pending(domain, first, world.owner) is True
    assert poll(domain, first).state is C.LeaseState.CANCELLED
    assert poll(domain, follower).grant is not None


def test_cancel_pending_registration_race_keeps_registered_work_live(case, world):
    domain = case.domain(slots=1, jobs=1)
    ticket, grant = _running(case, domain, world)

    assert cancel_pending(domain, ticket, world.owner) is False
    assert poll(domain, ticket).state is C.LeaseState.RUNNING
    assert grant is not None


def test_cancel_pending_wins_before_registration_without_killing_a_future_guard(case, world):
    domain = case.domain(slots=1, jobs=1)
    ticket = enqueue(domain, _request(case, domain, "unregistered"))
    grant = poll(domain, ticket).grant
    follower = enqueue(domain, _request(case, domain, "follower"))
    assert grant is not None

    assert cancel_pending(domain, ticket, world.owner) is True
    assert register_guard(domain, grant, world.guard) is False
    assert poll(domain, ticket).state is C.LeaseState.CANCELLED
    assert poll(domain, follower).grant is not None


def test_begin_finalization_returns_bound_proof_and_retains_claims(case, world, monkeypatch):
    domain = case.domain(slots=1, jobs=1)
    ticket, grant = _running(case, domain, world)
    follower = enqueue(domain, _request(case, domain, "follower"))
    claims = _sql(
        domain,
        "SELECT checkout_id,slots,memory_estimate,reserved_memory FROM jobs WHERE run_id=?",
        (grant.run_id,),
    )
    assert scheduler.mark_draining(domain, grant, _draining_guard(monkeypatch, world))
    _gone(world, world.guard)

    proof = begin_finalization(domain, grant)

    assert proof == C.QuiescenceProof(
        run_id=grant.run_id,
        generation=grant.generation,
        pgid=world.guard.pgid,
        checked_at=world.now,
        group_absent=True,
        escaped_survivors=False,
    )
    assert poll(domain, ticket).state is C.LeaseState.FINALIZING
    assert _sql(
        domain,
        "SELECT checkout_id,slots,memory_estimate,reserved_memory FROM jobs WHERE run_id=?",
        (grant.run_id,),
    ) == claims
    assert poll(domain, follower).grant is None


@pytest.mark.parametrize("state", ["GRANTED", "DRAINING"])
def test_begin_finalization_fails_closed_for_missing_guard(case, world, state):
    domain = case.domain(slots=1, jobs=1)
    ticket = enqueue(domain, _request(case, domain, "unregistered"))
    grant = poll(domain, ticket).grant
    follower = enqueue(domain, _request(case, domain, "follower"))
    assert grant is not None
    # DRAINING with no registered guard is malformed durable state; exercise
    # that guard check independently of the ordinary GRANTED state gate.
    if state == "DRAINING":
        _sql(domain, "UPDATE jobs SET state='DRAINING',phase='draining' WHERE run_id=?",
             (grant.run_id,))

    with pytest.raises(C.Problem) as caught:
        begin_finalization(domain, grant)

    assert caught.value.code == "ownership-uncertain"
    assert poll(domain, ticket).state.value == state
    assert poll(domain, follower).grant is None


@pytest.mark.parametrize("exists", [True, None])
def test_begin_finalization_fails_closed_for_live_or_ambiguous_group(
        case, world, monkeypatch, exists):
    domain = case.domain(slots=1, jobs=1)
    ticket, grant = _running(case, domain, world)
    follower = enqueue(domain, _request(case, domain, "follower"))
    assert scheduler.mark_draining(domain, grant, _draining_guard(monkeypatch, world))
    world.groups[world.guard.pgid] = exists

    with pytest.raises(C.Problem) as caught:
        begin_finalization(domain, grant)

    assert caught.value.code == "ownership-uncertain"
    assert poll(domain, ticket).state is C.LeaseState.UNCERTAIN
    assert poll(domain, follower).grant is None


def test_begin_finalization_fails_closed_for_escaped_descendant(case, world, monkeypatch):
    domain = case.domain(slots=1, jobs=1)
    ticket, grant = _running(case, domain, world)
    follower = enqueue(domain, _request(case, domain, "follower"))
    child = C.ProcessIdentity(pid=900002, birth=2.0, uid=os.getuid(), pgid=world.guard.pgid)
    world.identities[child.pid] = child
    world.children[world.guard.pid] = [child.pid]
    assert poll(domain, ticket).state is C.LeaseState.RUNNING
    assert scheduler.mark_draining(domain, grant, _draining_guard(monkeypatch, world))
    world.identities[child.pid] = replace(child, pgid=child.pid)
    world.children.clear()
    _gone(world, world.guard)

    with pytest.raises(C.Problem) as caught:
        begin_finalization(domain, grant)

    assert caught.value.code == "ownership-uncertain"
    assert poll(domain, ticket).state is C.LeaseState.UNCERTAIN
    assert _sql(domain, "SELECT slots FROM jobs WHERE run_id=?", (grant.run_id,)) == [(1,)]
    assert poll(domain, follower).grant is None


def test_finish_rechecks_scheduler_proof_and_retains_capacity_on_stale_proof(
        case, world, monkeypatch):
    domain = case.domain(slots=1, jobs=1)
    ticket, grant = _running(case, domain, world)
    follower = enqueue(domain, _request(case, domain, "follower"))
    assert scheduler.mark_draining(domain, grant, _draining_guard(monkeypatch, world))
    _gone(world, world.guard)
    proof = begin_finalization(domain, grant)
    stale = replace(proof, generation=proof.generation + 1)

    with pytest.raises(C.Problem):
        finish(domain, grant, stale, _final())

    assert poll(domain, ticket).state is C.LeaseState.UNCERTAIN
    assert poll(domain, follower).grant is None


def test_finish_releases_only_after_begin_finalization_proof_rechecks(case, world, monkeypatch):
    domain = case.domain(slots=1, jobs=1)
    ticket, grant = _running(case, domain, world)
    follower = enqueue(domain, _request(case, domain, "follower"))
    assert scheduler.mark_draining(domain, grant, _draining_guard(monkeypatch, world))
    _gone(world, world.guard)
    proof = begin_finalization(domain, grant)

    finish(domain, grant, proof, _final())

    assert poll(domain, ticket).state is C.LeaseState.RELEASED
    assert poll(domain, follower).grant is not None


@pytest.mark.parametrize("observer", ["poll", "reconcile"])
def test_begin_finalization_preserves_draining_across_follower_recovery(
        case, world, monkeypatch, observer):
    domain = case.domain(slots=1, jobs=1)
    ticket, grant = _running(case, domain, world)
    follower = enqueue(domain, _request(case, domain, "follower"))
    with monkeypatch.context() as guard_context:
        assert scheduler.mark_draining(domain, grant, _draining_guard(guard_context, world))
    _gone(world, world.guard)

    if observer == "poll":
        assert poll(domain, follower).grant is None
    else:
        before = domain.ledger.read_bytes()
        view = next(item for item in reconcile(domain) if item.run_id == grant.run_id)
        assert view.state is C.LeaseState.DRAINING
        assert view.phase == "draining"
        assert domain.ledger.read_bytes() == before
    assert _sql(domain, "SELECT state,phase FROM jobs WHERE run_id=?",
                (grant.run_id,)) == [("DRAINING", "draining")]

    proof = begin_finalization(domain, grant)
    assert poll(domain, follower).grant is None
    assert poll(domain, ticket).state is C.LeaseState.FINALIZING
    finish(domain, grant, proof, _final())
    assert poll(domain, ticket).state is C.LeaseState.RELEASED
    assert poll(domain, follower).grant is not None


@pytest.mark.parametrize("observer", ["none", "poll", "reconcile"])
def test_begin_finalization_rejects_guard_death_without_draining_handoff(
        case, world, observer):
    domain = case.domain(slots=1, jobs=1)
    ticket, grant = _running(case, domain, world)
    follower = enqueue(domain, _request(case, domain, "follower"))
    _gone(world, world.guard)

    if observer == "poll":
        assert poll(domain, follower).grant is None
    elif observer == "reconcile":
        view = next(item for item in reconcile(domain) if item.run_id == grant.run_id)
        assert view.state is C.LeaseState.RUNNING
        assert view.phase == "setup"
    with pytest.raises(C.Problem) as caught:
        begin_finalization(domain, grant)
    assert caught.value.code == "ownership-uncertain"
    assert poll(domain, ticket).state is C.LeaseState.RUNNING
    assert poll(domain, follower).grant is None
    assert _sql(domain, "SELECT state,phase FROM jobs WHERE run_id=?",
                (grant.run_id,)) == [("RUNNING", "setup")]


@pytest.mark.parametrize(
    "field,value",
    [("run_id", "b" * 32), ("nonce", "b" * 64), ("generation", 99),
     ("slots", 2), ("domain_id", "b" * 32), ("memory_estimate_mb", 10),
     ("reserved_memory_mb", 10)],
)
def test_begin_finalization_rejects_each_forged_grant_without_poisoning_handoff(
        case, world, monkeypatch, field, value):
    domain = case.domain(slots=1, jobs=1)
    ticket, grant = _running(case, domain, world)
    follower = enqueue(domain, _request(case, domain, "follower"))
    with monkeypatch.context() as guard_context:
        assert scheduler.mark_draining(domain, grant, _draining_guard(guard_context, world))
    _gone(world, world.guard)
    before = domain.ledger.read_bytes()

    with pytest.raises(C.Problem) as caught:
        begin_finalization(domain, replace(grant, **{field: value}))
    assert caught.value.code == "ownership-uncertain"
    assert domain.ledger.read_bytes() == before
    assert poll(domain, ticket).state is C.LeaseState.DRAINING
    assert poll(domain, follower).grant is None
    proof = begin_finalization(domain, grant)
    finish(domain, grant, proof, _final())
    assert poll(domain, follower).grant is not None


@pytest.mark.parametrize("observation", ["absent", "replaced", "unknown", "uid", "pgid"])
def test_begin_finalization_rejects_lost_owner_authority(
        case, world, monkeypatch, observation):
    domain = case.domain(slots=1, jobs=1)
    ticket, grant = _running(case, domain, world)
    follower = enqueue(domain, _request(case, domain, "follower"))
    with monkeypatch.context() as guard_context:
        assert scheduler.mark_draining(domain, grant, _draining_guard(guard_context, world))
    _gone(world, world.guard)
    if observation in {"absent", "unknown"}:
        world.identities.pop(world.owner.pid)
        if observation == "absent":
            world.absent.add(world.owner.pid)
    else:
        field, value = {"replaced": ("birth", world.owner.birth + 1),
                        "uid": ("uid", world.owner.uid + 1),
                        "pgid": ("pgid", world.owner.pgid + 1)}[observation]
        world.identities[world.owner.pid] = replace(world.owner, **{field: value})

    # All quiescence checks can pass: only owner authority rejects this call.
    with pytest.raises(C.Problem) as caught:
        begin_finalization(domain, grant)
    assert caught.value.code == "ownership-uncertain"
    assert _sql(domain, "SELECT state,slots,final_status FROM jobs WHERE run_id=?",
                (grant.run_id,)) == [("UNCERTAIN", 1, None)]
    assert _sql(domain, "SELECT state,nonce FROM jobs WHERE run_id=?",
                (follower.run_id,)) == [("QUEUED", None)]

    # Restore owner observation before recovery, which may separately release
    # an owner-absent lease. The failed finalization itself retains every claim.
    world.identities[world.owner.pid] = world.owner
    world.absent.discard(world.owner.pid)
    assert poll(domain, ticket).state is C.LeaseState.UNCERTAIN
    assert poll(domain, follower).grant is None
    with pytest.raises(C.Problem):
        begin_finalization(domain, grant)


def test_begin_finalization_rejects_boot_mismatch_without_mutation(case, world, monkeypatch):
    domain = case.domain(slots=1, jobs=1)
    ticket, grant = _running(case, domain, world)
    follower = enqueue(domain, _request(case, domain, "follower"))
    with monkeypatch.context() as guard_context:
        assert scheduler.mark_draining(domain, grant, _draining_guard(guard_context, world))
    _gone(world, world.guard)
    before = domain.ledger.read_bytes()

    with monkeypatch.context() as boot_context:
        boot_context.setattr(scheduler, "_boot_identity", lambda: "different-boot")
        with pytest.raises(C.Problem) as caught:
            begin_finalization(domain, grant)
    assert caught.value.code == "ownership-uncertain"
    assert domain.ledger.read_bytes() == before
    # Restore boot observation before polling: actual boot recovery is separate.
    assert poll(domain, ticket).state is C.LeaseState.DRAINING
    assert poll(domain, follower).grant is None


@pytest.mark.parametrize("grant_time", [None, "invalid", True])
def test_begin_finalization_rejects_malformed_grant_time(
        case, world, monkeypatch, grant_time):
    domain = case.domain(slots=1, jobs=1)
    ticket, grant = _running(case, domain, world)
    follower = enqueue(domain, _request(case, domain, "follower"))
    with monkeypatch.context() as guard_context:
        assert scheduler.mark_draining(domain, grant, _draining_guard(guard_context, world))
    _gone(world, world.guard)
    if grant_time is True:
        # SQLite coerces bound booleans to REAL 1.0. Inject True at the read
        # boundary to exercise the bool check rather than a valid float.
        original_open = scheduler._open_state

        def boolean_row(cursor, values):
            row = dict(zip((column[0] for column in cursor.description), values))
            if row.get("run_id") == grant.run_id and "grant_time" in row:
                row["grant_time"] = True
            return row

        def open_boolean_row(*args, **kwargs):
            conn, info = original_open(*args, **kwargs)
            conn.row_factory = boolean_row
            return conn, info

        monkeypatch.setattr(scheduler, "_open_state", open_boolean_row)
    else:
        _sql(domain, "UPDATE jobs SET grant_time=? WHERE run_id=?", (grant_time, grant.run_id))

    with pytest.raises(C.Problem) as caught:
        begin_finalization(domain, grant)
    assert caught.value.code == "ownership-uncertain"
    if grant_time is True:
        monkeypatch.setattr(scheduler, "_open_state", original_open)
    assert poll(domain, ticket).state is C.LeaseState.UNCERTAIN
    assert poll(domain, follower).grant is None
    assert _sql(domain, "SELECT slots,final_status FROM jobs WHERE run_id=?",
                (grant.run_id,)) == [(1, None)]


@pytest.mark.parametrize("winner", ["registration", "cancellation"])
def test_cancel_pending_registration_interleave_rechecks_state_after_lock(
        case, world, monkeypatch, winner):
    domain = case.domain(slots=1, jobs=1)
    ticket = enqueue(domain, _request(case, domain, "contended"))
    grant = poll(domain, ticket).grant
    follower = enqueue(domain, _request(case, domain, "follower"))
    assert grant is not None
    original_begin = scheduler._begin
    entered = []

    def commit_winner_before_lock(conn):
        # The losing call already opened the ledger. Commit the competing call
        # at its transaction boundary, then let it acquire the real SQLite lock.
        monkeypatch.setattr(scheduler, "_begin", original_begin)
        if winner == "registration":
            entered.append(register_guard(domain, grant, world.guard))
        else:
            entered.append(cancel_pending(domain, ticket, world.owner))
        original_begin(conn)

    monkeypatch.setattr(scheduler, "_begin", commit_winner_before_lock)
    if winner == "registration":
        assert cancel_pending(domain, ticket, world.owner) is False
        assert poll(domain, ticket).state is C.LeaseState.RUNNING
        assert poll(domain, follower).grant is None
        assert _sql(domain, "SELECT guard_pid FROM jobs WHERE run_id=?",
                    (grant.run_id,)) == [(world.guard.pid,)]
    else:
        assert register_guard(domain, grant, world.guard) is False
        assert poll(domain, ticket).state is C.LeaseState.CANCELLED
        assert poll(domain, follower).grant is not None
        assert _sql(domain, "SELECT guard_pid,nonce FROM jobs WHERE run_id=?",
                    (grant.run_id,)) == [(None, None)]
    assert entered == [True]


def test_partial_parallel_grant_and_queued_request(case, world):
    """A 4-slot request on a 2-slot machine grants 2; on a busy 4-slot
    machine the next 4-slot request queues instead of over-granting."""
    domain = case.domain(slots=2, jobs=2)
    ticket = enqueue(domain, _request(case, domain, "parallel", slots=4))
    grant = poll(domain, ticket).grant
    assert grant is not None
    assert grant.slots == 2

    busy = case.domain(slots=4, jobs=4)
    first = enqueue(busy, _request(case, busy, "first", slots=4))
    assert poll(busy, first).grant is not None
    assert register_guard(busy, poll(busy, first).grant, world.guard)
    second = enqueue(busy, _request(case, busy, "second", slots=4))
    queued = poll(busy, second)
    assert queued.grant is None
    assert queued.state is C.LeaseState.QUEUED
