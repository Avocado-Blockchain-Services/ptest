# Coverage under xdist (parallel coverage) — requirements

Status: requested by the user on 2026-09-25. Today `--cov` together with xdist makes ptest fall back to serial
("coverage (--cov) under xdist is out of scope; ptest runs serially", executability.py). Automatic selection needs the
pytest-cov coverage profile, so persea's api (xdist -n 4) must choose between parallel runs and selection.

## Goal
A pytest project with `--cov`/`--cov-report` AND xdist runs in parallel under ptest, and its coverage evidence qualifies
exactly as it does serially — so selection works with 4 workers.

## Requirements
1. Remove the "--cov under xdist → serial" fallback (executability, config/parallel tier, adapters/pytest). The parallel
   tier admits pytest-cov when the frozen pytest-cov/coverage tuple (runtime/pytest_bridge.py `_COVERAGE_TUPLE`) holds.
2. Coverage evidence in parallel: pytest-cov combines worker data on the xdist controller. The bridge already reads
   coverage from the controller; it must additionally verify that EVERY worker that ran tests contributed coverage data
   (use pytest-cov's xdist worker→controller data path, e.g. `workeroutput`/node finish hooks — read pytest-cov 7.x source
   in the venv to get it right). Missing or partial worker data → `coverage_complete = False` (fail closed for selection
   qualification). Coverage completeness never changes the test verdict itself.
3. Keep every existing parallel guarantee (verdict from bridge counts, collected-vs-run reconciliation across workers,
   refused rewriting hooks, owned worker processes, timeouts/Ctrl-C leave no survivors).
4. Update the user-facing text that describes the old tradeoff: executability reasons, project facts, doctor
   PARALLEL-001 / SELECTION-001 fixes (deterministic_items.py), help ("Coverage (--cov) under xdist …"), README, init's
   generated config (init must no longer write `-n 0` just because `--cov` is present). The SELECTION-001 fix for a parallel
   project becomes: add `--cov`/`--cov-report` and a `[selection]` policy; no serial tradeoff.
5. Tests (strict TDD, real subprocess twins with real pytest-xdist and pytest-cov in the dev env; add pytest-cov to the
   `test` extra via `uv add --optional test` if missing, keep the lock consistent):
   parallel run with coverage → parallel (N workers) + coverage complete; a worker killed mid-run → run not passed AND
   coverage incomplete; a worker whose coverage data is suppressed → coverage incomplete; selection qualifies after a
   parallel coverage run and then selects in parallel; a persea-shaped fixture (`-n 4 --dist=loadgroup -m "not x"` + `--cov`)
   runs with 4 workers and the correct label; serial coverage behaviour unchanged.

## Constraints
Keep it simple — delete the fallback code rather than layering. Public schemas change only additively. Tests through ptest
only. Never launch real claude/codex/opencode. Never modify persea.

## Design notes

Decisions taken while implementing (branch `cc-xdist-coverage`):

- The `--cov` tier fallback is deleted, not layered: `parallel_request`
  probes the project env for the pytest-cov/coverage pair
  (`coverage_environment_tuple`, sharing the new `_project_venv` /
  `_dist_info_versions` helpers with the xdist probe) and admits `--cov`
  only when the pair equals the frozen `pytest_bridge._COVERAGE_TUPLE`.
  Anything else stays serial with an environment reason
  (`config_level=False`), so `init` reports instead of writing `-n 0`.
- The bridge approves `pytest_cov` hooks only under a parallel grant
  (`OwnedPlugin._approved_hook_modules` gains `pytest_cov` when
  `workers >= 2`, plus an approved-module exemption in
  `_validate_parallel_transport_hooks` and a frozen-tuple gate in
  `_validate`). Serial basic + coverage still refuses exactly as before.
- Coverage completeness is an advanced-report flag only; the basic
  terminal report is untouched (no public schema change). In parallel,
  `finalize_evidence` requires the controller's combined measured files
  AND `cov_worker_node_id` (equal to the gateway id) in every granted
  worker's finished output — pytest-cov 7.1.0's `DistWorker.finish` sends
  it via `workeroutput` before `workerfinished`, and `DistMaster`
  reads the same dict, so the check follows the real data path. Missing
  or forged keys fail closed for selection qualification; the verdict
  never depends on the flag.
- The advanced profile runs in parallel: the controller builds its
  inventory from the first worker collection (file recovered as the
  longest leading `::` segment resolving to an in-checkout file; later
  workers must match; reconciliation re-verifies), outcomes come from
  forwarded worker reports, and `worker_identities` derives one identity
  per observed gateway from the executor-bound prefix (re-verified, not
  just reconciled). `run()` prepends the worker controls and reconciles
  for both profiles now.
- Bootstrap: an advanced FULL run follows the parallel tier (its complete
  evidence earns the stored parallel-identity profile); advanced
  scoped/selected runs without that consumed proof stay serial.
  `prepare_advanced` leads `-n N` before file positionals for selected
  runs so the bridge's argv-tail binding still holds. The stored profile
  persists `parallel_identity` (old files without the key read back as
  `False`); `compound_support` grants `parallel_identity` only from
  stored parallel evidence, never from the static catalog.
- Tests: bridge twins run under ptest via the suite interpreter (the dev
  `test` extra now carries the frozen tuple, so no skip); interpreter
  overrides (`PTEST_TEST_PYTHON_*`) are stripped by the guard, hence
  tuple-override e2e stays env-gated and the missing-tuple negative test
  uses a `PTEST_TEST_PYTHON_9_1_1_NOCOV` provisioned interpreter.
