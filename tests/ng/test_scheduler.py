"""Executable contracts for the local, fail-closed admission coordinator."""
from __future__ import annotations

import os
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import platform
import ptest.scheduler as scheduler
from ptest.scheduler import enqueue, finish, poll, reconcile, register_guard


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
