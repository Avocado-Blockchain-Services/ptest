### Task 4: Atomic shared admission and fail-closed recovery

**Own:** `src/ptest/scheduler.py`, `tests/ng/test_scheduler.py`, `tests/ng/fixtures/scheduler/`, `.pipeline/out/task-4.json`.

**Consumes:** Platform primitives and admission/grant records. **Produces:** exact enqueue/poll/register_guard/reconcile/finish signatures in design; no process launching.

Use T0's EffectiveLimits for read-only admission-limit presentation consumed by T11 where/status: validated max_slots/max_jobs together or both null, configured memory budget or null, repository ceiling or null. Do not invent a second public MachineLimits shape or create state to populate unknowns; test the shared record against absent state and configured-budget/default-no-budget cases.

- [ ] Write deterministic transaction tests before real concurrency:

```python
def test_budget_one_cannot_grant_two(case):
    domain = case.domain(slots=1, jobs=1)
    first = enqueue(domain, admission(case, domain, "first", slots=1))
    second = enqueue(domain, admission(case, domain, "second", slots=1))
    assert poll(domain, first).grant.slots == 1
    assert poll(domain, second).grant is None
```

`admission` is this test file's helper constructing AdmissionRequest with unique hex run IDs, real caller identity and distinct fixture checkouts; labels never become production IDs.

- [ ] Run `scripts/ptest-bootstrap tests/ng/test_scheduler.py -k budget_one`; implement fixed canonical shared-parent creation one component at a time with T0 ensure_shared_dir, then private coordination child/bootstrap lock/domain marker/SQLite, FIFO atomic admission, pending cap and persisted limits. Test first-use missing parents and existing0755 parents/0644 legacy siblings; preserve all existing modes/bytes, never chmod/prune. Guard registration CAS must win or lose atomically against revocation. AdmissionRequest.memory_mb is positive per-worker MiB or None (reject0); compute total estimate from actual slots, separately from reservation.
- [ ] Add multi-process SQLite races with event-log assertions (not sampled snapshots), checkout/global-lock contention, job/slot/memory bounds, head-of-line fairness, lowered limits, timeout/cancel, marker/DB replacement/corruption/full disk/protocol conflict and boot transitions. Two unknown-estimate fresh checkouts must overlap at default jobs2/no memory budget; configured-memory unknown reservation remains conservative/exclusive and visibly not a measurement. Advance a fake clock through two240s predecessors; default1800s third request must be admitted after480s, while explicit shorter deadlines still75. Assert status age/queue-wait fields. FINALIZING caller alive keeps lease although group absent; dead caller never promotes provisional success. Group exists/EPERM/reuse/escape uncertainty cannot release.
- [ ] Run `scripts/ptest-bootstrap tests/ng/test_scheduler.py`; commit `feat: coordinate fair atomic local admission and recovery`. Real guard crash/signal matrix completes in T7/T11, explicitly not claimed here.

