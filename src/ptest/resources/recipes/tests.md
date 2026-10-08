# Writing tests

A test earns its place by checking something someone decided. Before writing one,
answer the oracle question: what says this value is right? Acceptable answers are
a spec clause, a bug report, behavior the team deliberately pins, or an invariant
that must always hold. If the only answer is "the code returns it today", there is
no oracle yet; find one or do not write the test.

## Anti-patterns

- Mock-echo tests: the test arranges a mock, runs the code, and asserts only that a
  call happened. It proves the wiring you just wrote, not the behavior a caller
  depends on. Assert the observable result instead, and mock only what you do not own.
- Expected values copied from the implementation: pasting the output you got today
  into the assertion makes the test agree with the code by construction. Derive the
  expected value from the oracle, by hand or from an independent source.
- Unreviewed snapshots: a snapshot recorded and accepted without reading it pins
  whatever the code did, bugs included. Read every snapshot you accept, keep it small,
  and say in the test what it stands for.
- Assertion-less tests: a test that runs code and asserts nothing only proves it did
  not raise. Every test needs at least one assertion that fails when the behavior breaks.
- Tests of private functions: they freeze internals, break on every refactor and say
  nothing about behavior. Test through the public entry point that reaches the code.
- Import-only tests: importing a module, or constructing an object and asserting it
  exists, checks that Python can load a file. Test what the module does.

## Test budget

Write the tests the requirements need, not every test that is possible.

- One behavior test per requirement.
- One repro test per bug. Write it first and watch it fail for the reason in the bug
  report, then fix the bug and watch it pass. A repro test that never failed is unproven.
- One hostile-input table per trust boundary, holding only the rows that apply:
  empty or oversized input, traversal or injection, wrong owner, unauthenticated.
  Drop a row that cannot happen at that boundary instead of testing it anyway.
- One role x action authorization matrix per service that has authorization: every
  role against every action, with the allowed and the denied outcome spelled out.

A test outside this budget needs its own named oracle.

## Coverage

A coverage gate is a floor, not a target. Keep the project's existing gate as it is.
Do not add tests to raise coverage, and do not lower the gate, add omit patterns or add
`pragma: no cover` lines to make it pass.

When the gate fails, work out which behavior is untested and test it, or fix the gate by
deleting dead code that nothing reaches. If neither fits, report the gap to the user. Never satisfy the
gate with assertion-free tests: they execute lines without checking anything.

## Example: a hostile-input table (pytest)

One parametrized test, one row per hostile case that applies at this boundary.

```python
import pytest

HOSTILE = [
    pytest.param("", id="empty"),
    pytest.param("x" * 100_000, id="oversized"),
    pytest.param("../../etc/passwd", id="traversal"),
    pytest.param("'; drop table users; --", id="injection"),
]


@pytest.mark.parametrize("name", HOSTILE)
def test_upload_rejects_hostile_names(client, name):
    response = client.post("/upload", json={"name": name})
    assert response.status_code == 422
    assert not stored_files()
```

## Example: a hostile-input table (vitest)

```js
import { test, expect } from "vitest";

test.each([
  ["empty", ""],
  ["oversized", "x".repeat(100000)],
  ["traversal", "../../etc/passwd"],
])("upload rejects a hostile name: %s", async (_label, name) => {
  const response = await upload({ name });
  expect(response.status).toBe(422);
  expect(storedFiles()).toHaveLength(0);
});
```
