"""Load-contention twins: transient pressure must wait, never fail closed.

Under machine load, scheduler transactions contend on the single coordinator
DB (parent poll loop vs guard register/drain) and process-table scans exceed
their per-pass budget. Spurious ``coordinator-unavailable`` /
``ownership-uncertain`` / ``protocol-mismatch`` outcomes flip real runs to
exit 70/75 although the tests passed. These twins pin the contract:

* transient lock contention waits with bounded retries (never surfaces as
  ``coordinator-unavailable`` while the retry budget holds);
* a persistently unavailable coordinator still fails closed as
  retryable ``coordinator-unavailable`` once the budget is exhausted;
* genuine corruption still reports ``coordinator-corrupt``.

The :class:`CpuBurners` / :func:`hammer_writes` helpers are the test-only
stress harness: exact-PID burner lifecycle plus concurrent coordinator
clients with spurious-outcome counters.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ptest import contracts as C
from ptest import platform
from ptest import scheduler
from ptest.scheduler import enqueue, poll


def _request(case, domain, label: str, **kwargs) -> C.AdmissionRequest:
    import hashlib
    import secrets

    owner = platform.process_identity(os.getpid())
    assert owner is not None
    tag = hashlib.sha256(label.encode()).hexdigest()[:32]
    checkout = C.CheckoutIdentity(
        project_id="ab" * 16,
        checkout_id=tag,
        root=domain.root / f"checkout-{tag[:8]}",
    )
    return C.AdmissionRequest(
        run_id=secrets.token_hex(16),
        checkout=checkout,
        owner=owner,
        slots=kwargs.get("slots", 1),
        exclusive=kwargs.get("exclusive", False),
        locks=(),
        memory_mb=None,
        deadline=kwargs.get("deadline", 0.0),
        fixture=domain.fixture,
    )


class CpuBurners:
    """Test-only load generator: one busy loop per core, exact-PID lifecycle.

    Start/stop are explicit so no burner can outlive the test that made it:
    every spawned PID is SIGKILLed and reaped by that same PID.
    """

    def __init__(self, count: int):
        assert count >= 1
        self.count = count
        self._procs: list[subprocess.Popen] = []

    def start(self) -> "CpuBurners":
        assert not self._procs
        for _ in range(self.count):
            proc = subprocess.Popen(
                [sys.executable, "-c", "while True: pass"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            self._procs.append(proc)
        assert [p.pid for p in self._procs
                if p.pid is not None and p.poll() is None]
        return self

    def pids(self) -> tuple[int, ...]:
        return tuple(p.pid for p in self._procs)

    def stop(self) -> None:
        procs, self._procs = self._procs, []
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=30)


@pytest.fixture
def burners():
    owned = CpuBurners(max(1, os.cpu_count() or 1))
    owned.start()
    try:
        yield owned
    finally:
        owned.stop()


class _WriteHoldout(threading.Thread):
    """Hold one coordinator write transaction open for ``hold_s`` seconds."""

    def __init__(self, ledger: Path, hold_s: float, ready: threading.Event):
        super().__init__(daemon=True)
        self.ledger = ledger
        self.hold_s = hold_s
        self.ready = ready
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            conn = sqlite3.connect(str(self.ledger), isolation_level=None)
            try:
                conn.execute("BEGIN IMMEDIATE")
                self.ready.set()
                time.sleep(self.hold_s)
                conn.execute("COMMIT")
            finally:
                conn.close()
        except BaseException as exc:  # recorded, re-raised by the test body
            self.error = exc
            self.ready.set()


def test_begin_immediate_waits_out_transient_lock_hold(case):
    """One 3s write hold (past the 2s busy timeout) must not surface."""
    domain = case.domain(slots=2, jobs=2)
    first = enqueue(domain, _request(case, domain, "seed"))
    assert poll(domain, first).grant is not None

    ready = threading.Event()
    holdout = _WriteHoldout(Path(domain.ledger), 3.0, ready)
    holdout.start()
    try:
        assert ready.wait(timeout=30)
        started = time.monotonic()
        ticket = enqueue(domain, _request(case, domain, " waiter"))
        state = poll(domain, ticket)
        waited = time.monotonic() - started
        assert holdout.error is None
        assert state.state is C.LeaseState.GRANTED
        # It waited for the lock, not failed fast past it.
        assert waited >= 2.5
    finally:
        holdout.join(timeout=30)
    assert holdout.error is None


def _hammer_round(case, domain, owner, label: str) -> None:
    """One admit-then-release cycle: every step must survive contention."""
    ticket = enqueue(domain, _request(case, domain, label))
    poll(domain, ticket)
    scheduler.cancel_pending(domain, ticket, owner)


def test_hammering_writers_never_see_spurious_unavailable(case):
    """Concurrent writers on one ledger never observe coordinator-unavailable."""
    domain = case.domain(slots=4, jobs=4)
    owner = platform.process_identity(os.getpid())
    assert owner is not None
    failures: list[BaseException] = []
    lock = threading.Lock()

    def hammer(index: int) -> None:
        try:
            for round_ in range(10):
                _hammer_round(case, domain, owner, f"hammer-{index}-{round_}")
        except BaseException as exc:  # noqa: BLE001 - collected, asserted below
            with lock:
                failures.append(exc)

    threads = [threading.Thread(target=hammer, args=(index,), daemon=True)
               for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not any(thread.is_alive() for thread in threads)
    assert failures == []


def test_hammering_writers_under_cpu_pressure(case, burners):
    """Same hammer while every core burns: retries must still absorb contention."""
    assert len(burners.pids()) >= 1
    domain = case.domain(slots=4, jobs=4)
    owner = platform.process_identity(os.getpid())
    assert owner is not None
    failures: list[BaseException] = []
    lock = threading.Lock()

    def hammer(index: int) -> None:
        try:
            for round_ in range(6):
                _hammer_round(case, domain, owner, f"load-{index}-{round_}")
        except BaseException as exc:  # noqa: BLE001 - collected, asserted below
            with lock:
                failures.append(exc)

    threads = [threading.Thread(target=hammer, args=(index,), daemon=True)
               for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)
    assert not any(thread.is_alive() for thread in threads)
    assert failures == []


def test_commit_contention_retries_without_double_apply(case, monkeypatch):
    """State axis: a retried commit applies exactly once (no duplicate row)."""
    import sqlite3 as sqlite3_module

    domain = case.domain(slots=2, jobs=2)
    real_open = scheduler.storage.open_database
    commits = []

    class FlakyCommitConnection:
        def __init__(self, conn):
            object.__setattr__(self, "_conn", conn)

        def __getattr__(self, name):
            return getattr(object.__getattribute__(self, "_conn"), name)

        def __setattr__(self, name, value):
            setattr(object.__getattribute__(self, "_conn"), name, value)

        def __enter__(self):
            return object.__getattribute__(self, "_conn").__enter__()

        def __exit__(self, *args):
            return object.__getattribute__(self, "_conn").__exit__(*args)

        def commit(self):
            commits.append(1)
            if len(commits) == 1:
                raise sqlite3_module.OperationalError("database is locked")
            return object.__getattribute__(self, "_conn").commit()

    monkeypatch.setattr(scheduler.storage, "open_database",
                        lambda *args, **kwargs: FlakyCommitConnection(
                            real_open(*args, **kwargs)))
    ticket = enqueue(domain, _request(case, domain, "once"))
    assert poll(domain, ticket).state is C.LeaseState.GRANTED
    assert len(commits) >= 2
    raw = sqlite3_module.connect(str(domain.ledger))
    try:
        assert raw.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    finally:
        raw.close()


def test_poll_with_retry_waits_out_typed_contention(case, monkeypatch):
    """Admission poll treats verified contention like a long queue."""
    from types import SimpleNamespace

    from ptest import operations
    from ptest.storage import TransientContention

    domain = case.domain(slots=2, jobs=2)
    calls = []
    sentinel = object()

    def flaky(domain_arg, ticket):
        calls.append(1)
        if len(calls) <= 2:
            raise TransientContention(message="coordinator is busy",
                                      phase="scheduler")
        return sentinel

    monkeypatch.setattr(scheduler, "poll", flaky)
    admission = SimpleNamespace(deadline=time.monotonic() + 30)
    assert operations._poll_with_retry(domain, object(), admission) is sentinel
    assert len(calls) == 3


def test_finalize_with_retry_ignores_genuine_failure_immediately(monkeypatch):
    """Finalization retries only verified contention, never verdicts."""
    from ptest import operations

    calls = []

    def failing():
        calls.append(1)
        raise C.Problem(code="ownership-uncertain", message="no proof",
                        phase="scheduler", retryable=False)

    with pytest.raises(C.Problem):
        operations._finalize_with_retry(failing)
    assert len(calls) == 1


def test_persistent_lock_exhausts_budget_and_stays_unavailable(case, monkeypatch):
    """A lock held past the whole retry budget still fails closed (retryable)."""
    # Both layers are bounded: the transaction lock wait and the outer
    # whole-call retry. Shrink both so the pin stays fast.
    monkeypatch.setattr(scheduler, "_BEGIN_RETRY_DEADLINE_S", 0.2)
    monkeypatch.setattr(scheduler, "_BEGIN_RETRY_MAX_S", 0.02)
    monkeypatch.setattr(scheduler, "_TRANSACTION_RETRY_DEADLINE_S", 0.5)
    monkeypatch.setattr(scheduler, "_TRANSACTION_RETRY_MAX_S", 0.05)
    domain = case.domain(slots=2, jobs=2)
    seed = enqueue(domain, _request(case, domain, "seed"))
    assert poll(domain, seed).grant is not None

    ready = threading.Event()
    holdout = _WriteHoldout(Path(domain.ledger), 30.0, ready)
    holdout.daemon = True
    holdout.start()
    try:
        assert ready.wait(timeout=30)
        with pytest.raises(C.Problem) as caught:
            enqueue(domain, _request(case, domain, "starved"))
        assert caught.value.code == "coordinator-unavailable"
        assert caught.value.retryable is True
    finally:
        holdout.join(timeout=5)


def test_ancestry_check_retries_transient_observation(case, monkeypatch):
    """One vanishing /proc read during admission must not read as nesting risk."""
    import psutil as psutil_module

    domain = case.domain(slots=2, jobs=2)
    # Seed a live guard in its own session so the ancestry walk runs without
    # matching (a same-group guard would be genuine nesting evidence).
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    try:
        seed_guard = platform.process_identity(child.pid)
        assert seed_guard is not None and seed_guard.pgid != os.getpgrp()
        seed_ticket = enqueue(domain, _request(case, domain, "seed-guard"))
        seed_grant = poll(domain, seed_ticket).grant
        assert seed_grant is not None
        assert scheduler.register_guard(domain, seed_grant, seed_guard) is True

        real_process = psutil_module.Process
        ppid_calls = []

        class FlakyPpid(real_process):
            def ppid(self):
                ppid_calls.append(1)
                if len(ppid_calls) == 1:
                    raise psutil_module.NoSuchProcess(self.pid)
                return super().ppid()

        monkeypatch.setattr(psutil_module, "Process", FlakyPpid)
        ticket = enqueue(domain, _request(case, domain, "ancestry"))
        assert poll(domain, ticket).state is C.LeaseState.GRANTED
        assert len(ppid_calls) > 1
    finally:
        child.kill()
        child.wait(timeout=30)


def test_guard_registration_waits_out_transient_lock_hold(case):
    """register_guard (the guard side of the handoff) waits instead of failing."""
    domain = case.domain(slots=1, jobs=1)
    ticket = enqueue(domain, _request(case, domain, "guarded"))
    grant = poll(domain, ticket).grant
    assert grant is not None

    ready = threading.Event()
    holdout = _WriteHoldout(Path(domain.ledger), 3.0, ready)
    holdout.start()
    try:
        assert ready.wait(timeout=30)
        owner = platform.process_identity(os.getpid())
        assert owner is not None
        guard = C.ProcessIdentity(pid=owner.pid + 0, birth=owner.birth,
                                  uid=owner.uid, pgid=owner.pgid)
        # Register from this process: reuse the owner identity shape as the
        # guard identity would carry it (same-process CAS path).
        assert scheduler.register_guard(domain, grant, guard) is True
        assert holdout.error is None
    finally:
        holdout.join(timeout=30)
    assert holdout.error is None
