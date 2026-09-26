# ptest rules for coding agents

Run tests through `ptest` from the repository root; never invoke pytest, vitest, npm test,
go test, or cargo test directly. After each edit run the default loop `ptest --changed`;
target a child with its declared prefix, such as `ptest api/tests/ng/test_x.py`. Child `.ptest.toml` files
remain authoritative; never copy, merge, or rewrite them. Run `ptest init` at the root.
Run `ptest --full` once after the integrated change; the first `ptest --changed` may run everything to record a baseline.

Qualified pytest projects run xdist in parallel under ptest (one worker per `-n N`, or
per granted slot for `-n auto`); unqualified projects run serially with ptest's generated `-n 0`.
`ptest init` writes `-n 0` into a new config only for static fallbacks; it never rewrites an existing config.
Keep `-n N`, `--dist`, `--tx` out of ptest args; `-n 0` opts out of the parallel tier.
Vitest runs one exclusive `vitest run` and manages its own workers. Declared `[setup]`
(such as `npm ci`) runs first when required paths are missing or the lockfile changed.

`ptest doctor` asks consent before review, then makes one initial call per model-assessed
item and one bounded verification for each valid reply, including OK replies. Timing,
selection and parallel-execution items use ptest's own facts. Defaults are requested
models Codex `gpt-6-sol` and Claude `opus`, unless explicitly overridden. Reviews cover
cited reachable units; omitted decisive callers or failure paths remain unknown.
`ptest doctor --offline` is static and sends nothing.

Keep tests fast and deterministic. Use factories/builders for test records and small fixtures
(function by default; session only for expensive read-only infrastructure), with no mutable shared fixture state.
Every record has an owner that cleans it up. Inspect tests
above 0.5 seconds; optimize ordinary tests at 2 seconds; investigate tests above 3 seconds
unless an integration boundary justifies them. See `ptest guide` recipes:
factories, databases, cache, files-ports, processes, time-network.

Reuse expensive server/schema setup per run or worker; a fresh SQLite database
or mutable instance per test/use can provide clear ownership. Namespace shared database
records by overlapping run or worker owners. Namespace shared external caches by their
overlapping owners; a fresh per-test/per-use instance can own local cache state. Delete
only owned records; never globally flush caches or drop databases by name alone.
Avoid shared fixed paths, fixed ports, detached children, live network targets and
wall-clock sleeps for synchronization.

Treat findings as hypotheses, not suite-wide certificates. Claims cover cited reachable
callers and mechanisms; omitted decisive callers or failure paths remain unknown.
Preserve assertions, coverage, test inventory and unrelated user changes. Report the
exact ptest command, result, remaining failures and untested scope.

Requesting doctor, guide or a prompt grants assessment authority only. Source repair
requires a separate user instruction; never treat assessment as permission to edit or
a filled worksheet as updated ptest readiness. Keep `unknown` until each row has evidence.
