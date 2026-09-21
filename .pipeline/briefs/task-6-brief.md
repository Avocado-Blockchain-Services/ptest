### Task 6: Exact-spec Vitest bridge and additive reporter

**Own:** `src/ptest/adapters/vitest.py`, `src/ptest/runtime/vitest_bridge.mjs`, `tests/ng/test_vitest_adapter.py`, `tests/ng/fixtures/vitest/` (including package.json/package-lock.json), `.pipeline/out/task-6.json`. No shared lock/registry edits.

**Consumes:** PreparedRun, generated bridge protocol, exact design v3 public flow. **Produces:** `prepare(config,plan,grant,attempt)->PreparedRun` and Node bridge; advanced fixture Vitest/coverage-v8 3.2.7 plus basic serial fixture pairs3.1.4/3.2.6/3.2.7. T6 explicitly generates its owned package-lock.json files with `npm install --package-lock-only --ignore-scripts --no-audit --no-fund` inside each dedicated fixture directory; this authorized metadata-only dependency step is recorded with possible registry network and must create no node_modules or run lifecycle scripts. Actual npm ci installation remains candidate ptest's declared setup at T11, not hand-installed/shared.

- [ ] Write safe preparation/bounds fixture:

```python
def test_vitest_worker_env_is_owned(case):
    prepared = prepare(case.config(runner_kind="vitest", workers=2),
                       full_plan(case), grant(case, 2), attempt(case))
    updates = dict(prepared.env_updates)
    assert updates["VITEST_MAX_FORKS"] == "2"
    assert updates["VITEST_MIN_FORKS"] == "2"
    assert prepared.summary.workers == 2
```

- [ ] Run `scripts/ptest-bootstrap tests/ng/test_vitest_adapter.py -k worker_env`; implement parseCLI array, one createVitest instance, pre-init noAPI/watch/browser controls, exact project/pool validation, public configureVitest inline reporter append, exact specifications, init/runTestSpecifications/close and truthful full coverage.
- [ ] Add native_cli fixtures for config/env pool caps, concurrent test count, duplicate titles, two similarly named path substrings, literal filters, custom reporters retained, coverage failure, single project, rejected workspace/custom pool/typecheck/watch/API/untested configureVitest hook, missing module/version mismatch, snapshot/config auto-update =>changed-input incomplete. Basic_serial3.1.4/3.2.6/3.2.7 uses guarded create→native start→close (never init+start), effective all bounds/maxConcurrency1, grant1/nonexclusive and no selection/baseline. Matching coverage-v8 and terminal failure semantics must survive. Versions outside the table/custom unsafe controls reject; no guessed compatibility. Probe distinct worker IDs only where supported; no shared w000 fallback.
- [ ] Run unit scope `scripts/ptest-bootstrap tests/ng/test_vitest_adapter.py -k 'not native_cli'`; commit `feat: add exact-spec Vitest bridge with guarded worker controls`. Unavailable3.2.7/Node/macOS evidence stays explicit; T11 executes native_cli through ptest before promotion.

