# ptest rules for coding agents

Run every test command through `ptest`; do not call pytest, Vitest, npm test,
Go test, or Cargo test directly. During iteration run the smallest relevant
scope. Run `ptest --full` once after the integrated change.

Keep ordinary tests fast and deterministic. Prefer factories/builders for test
records. Inspect frequent tests above 0.5 seconds; optimize ordinary tests that
reach 2 seconds and investigate anything above 3 seconds unless it is a
documented integration boundary.

For a database, create expensive server, schema, or template setup once per run
or worker. Use one database per worker per run, never one database per test.
Reset records owned by the test and make cleanup ownership explicit; never drop
a database merely because its name looks like a test database.

Namespace Redis, Valkey, and every cache by run and worker. Delete only that
owned namespace. Never use global cache flush, blanket database drop, shared
fixed paths, fixed ports, detached child processes, live network targets, or
wall-clock sleeps for synchronization.

Treat `ptest doctor` findings as hypotheses, not proof. Verify the cause and
callers before repairing code. Preserve assertions, coverage, test inventory,
and unrelated user changes. Report the exact ptest command, result, remaining
failures, and untested scope.
