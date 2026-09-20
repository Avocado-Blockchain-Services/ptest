# Task 12c implementation brief

## Ownership

Production: `src/ptest/operations.py`, `src/ptest/cli.py`.

Bounded frozen-API fixes only: `src/ptest/selection.py` and
`src/ptest/history.py`, with their owned regression tests.

Tests: new `tests/ng/test_shadow.py`, new `tests/ng/test_probes.py`, existing
`tests/ng/test_cli.py`, existing `tests/ng/test_operations.py`.

Fixtures: new `tests/ng/fixtures/shadow/`, new `tests/ng/fixtures/probes/`.

Evidence: `.pipeline/out/task-12c.json` and final `.pipeline/out/task-12.json`.

Excluded: public `execute`/`main` signature changes, shared conftest/support,
adapters/bridges/runners/reports/contracts changes except bounded frozen-API
compatibility fixes, root dependencies, remote/cloud/API/model/product-resource
actions, second executors, and commits.

## Acceptance criteria

- S1 uses a real complete native selected failure, still executes full, retains
  the first nonzero/raw outcomes, and never substitutes adapter/collection or
  refusal errors.
- S2 uses a committed four-file alpha/beta/gamma/delta fixture, deliberately
  wrong mapping, `full_ratio=0.75`, selected alpha pass plus omitted beta full
  failure, persistent `selection-shadow-quarantine`, automatic full fallback,
  explicit scoped reproduction, corrected-policy/new-baseline recovery, and
  `full_ratio=0.50` equality widening to full.
- S3 prevents later launches and quarantine clearing on source invalidation,
  decision/compound timeout, missing report/handoff, or incomplete full
  evidence; stable matching runs retain raw evidence and may exceed the
  30-second probe default within the 600-second shadow budget.
- P1-P3 enforce probe grammar, scope/in-root validation, attempt timeout
  bounds (1/120), repeat/worker bounds, isolation and capacity checks, FIFO
  queue/capping/cancellation, serial/parallel identity and namespace
  preservation, and no launches after cancellation/EOF/invalidation/failure.
- Probe runs only when Q-PY-PROBE or Q-VT-PROBE is actually qualified. The
  current T12b evidence says both are `unsupported-capability`, so probe must
  fail closed without fake identities, serial substitutes, baseline writes,
  quarantine changes, or fabricated conflict/no-conflict observations.
- C1 accepts all automatic shadow spellings and rejects mixed shadow/full,
  shadow/scoped, probe/static options, and unsafe numeric/scope inputs with
  exact closed reason codes and redacted diagnostics.
- C2 preserves finite attempts, null unknown timing/count fields, raw exit
  codes, redacted summaries, and real reasons; uses one lease/guard and the
  typed history publication APIs only. No private marker helpers or baseline
  publication from shadow/probe.

## Verification

Use only `ptest` for tests. Run scoped evidence for the owned test modules,
`git diff --check`, and `graphify update .` after source changes. Do not run
`ptest --full`; root owns the final integrated full suite. Record every actual
result, known flakes/limitations, and the two unsupported probe qualifications
in `.pipeline/out/task-12c.json` and `.pipeline/out/task-12.json`.
