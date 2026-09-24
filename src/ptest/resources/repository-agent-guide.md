# ptest rules for coding agents

Run every test command through `ptest` from the repository root.
Never invoke pytest, vitest, npm test, go test, or cargo test directly. For focused work, prefix
the scope with its declared child, such as `ptest api/tests/ng/test_x.py`. Child `.ptest.toml` files
remain authoritative; never copy, merge, or rewrite them, and never bypass them by changing
directories. Run `ptest init` from the repository root. Run `ptest --full` once after the integrated change.

Qualified pytest projects run xdist in parallel under ptest (one worker per `-n N`,
or per granted slot for `-n auto`); unqualified projects run serially with ptest's
generated `-n 0`. `ptest init` writes `-n 0` into a new config only for static
fallbacks and never rewrites an existing config. Keep `-n N`, `--dist`, `--tx` out
of ptest args; `-n 0` in `[runner] args` opts out of the parallel tier.

Vitest runs as one exclusive `vitest run` command and manages its own workers; ptest
reports only its exit code. Declared `[setup]` (such as `npm ci`) runs first when
required paths are missing or the lockfile changed.

`ptest doctor` asks for consent before any model review, then sends one cheap-model
call per checklist item that needs one; timing, selection and parallel execution
items skip the model. `ptest doctor --offline` is static only and sends nothing.

Keep ordinary tests fast and deterministic. Use factories/builders for test records; keep fixtures
small and scoped (function by default; session only for expensive read-only infrastructure)
with no mutable shared fixture state. Every created record has an owner that cleans it up.
Inspect tests above 0.5 seconds; optimize ordinary tests at 2 seconds and investigate
anything above 3 seconds unless a documented integration boundary says otherwise. See
`ptest guide` recipes: factories, databases, cache, files-ports, processes, time-network.

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
