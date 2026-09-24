# ptest rules for coding agents

Run every test command through `ptest` from the repository root.
Never invoke pytest, vitest, npm test, go test, or cargo test directly. For focused work, prefix
the scope with its declared child, such as `ptest api/tests/ng/test_x.py`. Child `.ptest.toml` files
remain authoritative; never copy, merge, or rewrite them, and never bypass them by changing
directories. Run `ptest init` from the repository root.
Run `ptest --full` once after the integrated change.

Pytest runs serially under ptest: `ptest init` writes `-n 0` into a new pytest config
when the project enables xdist. An existing config is never rewritten — init reports it as not runnable
with the exact fix; the serial `-n 0` in `[runner] args` is the only allowed xdist control.
Never add `-n N`, `--dist`, `--tx`, or other parallel controls to ptest args.

Vitest runs as one exclusive `vitest run` command and manages its own workers; ptest
reports only its exit code. Declared `[setup]` (such as `npm ci`) runs first when
required paths or the setup fingerprint are missing.

`ptest doctor` asks for consent before any model review, then sends
one cheap-model call per checklist item; `ptest doctor --offline` is static only and
sends nothing.

Keep ordinary tests fast and deterministic. Prefer factories/builders for test records.
Inspect tests above 0.5 seconds; optimize ordinary tests at 2 seconds and investigate
anything above 3 seconds unless a documented integration boundary says otherwise.

For a database, create expensive setup once per run or worker. Use one database per worker per run,
never one database per test. Reset records owned by the test and make cleanup ownership explicit;
never drop a database merely because its name looks like a test database.

Namespace Redis, Valkey, and every cache by run and worker. Delete only that owned namespace.
Never use global cache flush, blanket database drop, shared fixed paths, fixed ports, detached
child processes, live network targets, or wall-clock sleeps for synchronization.

Treat `ptest doctor` findings as hypotheses, not proof. Verify the cause and callers before
repairing code. Preserve assertions, coverage, test inventory, and unrelated user changes.
Report the exact ptest command, result, remaining failures, and untested scope.

Requesting doctor, guide, or a prompt grants assessment authority only. Source repair requires
a separate user instruction; never treat an assessment as permission to edit or a filled
worksheet as updated ptest readiness. Fill one copy per repository from direct evidence,
keeping `unknown` until each row has evidence.

If a merge is fast-forward and the exact tip commit already passed the required ptest gate, do not rerun ptest solely because of the merge. A merge commit, new changes, or an untested tip still requires the applicable ptest gate.
After source merges, run `graphify update .`; skipping duplicate ptest does not skip the graph refresh.
