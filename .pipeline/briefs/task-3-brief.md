### Task 3: Durable outcome history, baseline and failure obligations

**Own:** `src/ptest/history.py`, `tests/ng/test_history.py`, `.pipeline/out/task-3.json`.

**Consumes:** explicit DomainPaths, RunResult/Inventory/InputSnapshot/Baseline/Obligation/HistoryView/PublishResult and T0 files/storage. **Produces:** `read_history(domain,checkout)->HistoryView`, `publish_outcome(domain,checkout,result,inventory)->PublishResult`. Mutable state is domain+checkout scoped, independent of scheduler DB. No platform import/env lookup/normal-root fallback; wave1 unit tests use T0 case.domain directly.

- [ ] Write a failing obligation-preservation assertion:

```python
def test_skip_does_not_clear_failure(case):
    domain = case.domain()
    checkout = case.checkout(domain)
    publish_outcome(domain, checkout, case.result(sequence=1, status="failed"),
                    case.inventory(("tests/test_a.py",), outcome="failed"))
    publish_outcome(domain, checkout, case.result(sequence=2, status="passed"),
                    case.inventory(("tests/test_a.py",), outcome="skipped"))
    assert read_history(domain, checkout).obligations[0].file == "tests/test_a.py"
```

- [ ] Run `scripts/ptest-bootstrap tests/ng/test_history.py -k skip_does_not_clear`; implement transactional immutable summaries, failure upsert/reconciliation, clean complete baseline eligibility and bounded retention.
- [ ] Add explicit coverage-only full obligation, old late pass, incompatible branch/config, teardown failure, deleted ID full-inventory reconciliation, dirty/mixed-source result, interrupted report, different checkout/domain, and prune/quota/corrupt-store cases. Assert fixture history operations never touch a normal-state sentinel; absent read_history creates no directories/DB/key. Basic serial unknown IDs cannot clear existing per-test obligations or seed a baseline. Compaction preserves protected data or disables selection. No raw argv/env storage.
- [ ] Run `scripts/ptest-bootstrap tests/ng/test_history.py`; commit `feat: retain failure obligations and truthful local baselines`.

