# ptest rules for coding agents

Run `ptest` from the monorepo root—the directory containing `AGENTS.md` and
`CLAUDE.md`. For focused work, prefix the scope with its declared child, such
as `ptest api/tests/ng/test_x.py`; use root `ptest --full` for the integrated
gate. Child `.ptest.toml` files remain authoritative and must not be copied,
merged, rewritten, or bypassed by changing directories.

Run `ptest init` from the repository root when setting up a new checkout. It
creates the root dispatcher and missing child configs after bounded validation;
do not hand-edit or duplicate child configuration.

Run every test command through `ptest`; do not call pytest, Vitest, npm test,
Go test, or Cargo test directly. During iteration run the smallest relevant
scope. Run `ptest --full` once after the integrated change.

If a merge is fast-forward and the exact tip commit already passed the required ptest gate, do not rerun ptest solely because of the merge. A merge commit, new changes, or an untested tip still requires the applicable ptest gate.
After source merges, run `graphify update .`; skipping duplicate ptest does not skip the graph refresh.

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

Requesting doctor, guide, or a prompt grants assessment authority only: inspect
and report. Source repair requires a separate user instruction granting repair
authority; never treat an assessment as permission to edit, and never present a
filled worksheet as updated ptest readiness. Fill one worksheet copy per
repository from direct source, config, and runtime evidence, keeping
`unknown` until the evidence for that row is in hand.
