# ptest test policy

This repository opted in to this stricter stance on tests. `docs/ptest-agent.md`
still says how to run them.

## Oracle first

Do not add a test without a named oracle: a spec clause, a bug report, behavior
the team deliberately pins, or an invariant. Do not write a test whose expected
value was read off the implementation.

## Test budget

- One behavior test per requirement.
- One repro test per bug, written first and seen failing before the fix.
- One table-driven hostile-input test per trust boundary, with only the rows
  that apply: empty/oversized, traversal/injection, wrong owner, unauthenticated.
- One role x action authorization matrix per service that has authorization.

A test outside this budget needs its own named oracle.

## Coverage

The coverage gate is a floor, not a target. Do not add tests to raise coverage.
When the gate fails, test real untested behavior or delete dead code; if neither
fits, report the gap. Never satisfy the gate with assertion-free tests, new
`pragma: no cover` lines, or new omit patterns.

Full checklist and examples: `ptest guide tests`.
