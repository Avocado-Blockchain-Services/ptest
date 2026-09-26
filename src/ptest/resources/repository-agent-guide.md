# ptest rules for coding agents

Run every test command through `ptest` from the repository root (the directory
holding the root `.ptest.toml`). Never invoke pytest, vitest, `npm test`,
`go test`, or `cargo test` directly.

## The loop

| Situation | Command |
|---|---|
| After each edit | `ptest` (bare `ptest` runs the changed tests) |
| One test file | `ptest <path>` |
| Integrated change, before handoff | `ptest --full` once |
| Force a full rerun over already-verified inputs | `ptest --full --again` |

A scoped or changed green is iteration only; only `ptest --full` completes the
change. Never rerun `ptest --full` without a change: `--full` skips
already-verified inputs, and a duplicate full run joins the running full run
instead of starting a second one.

## Monorepo

Always run from the monorepo root. Prefix scopes with the owning child, such
as `ptest api/tests/test_example.py`. Never cd into a child to run tests.
Child `.ptest.toml` files remain authoritative; never copy, merge, or rewrite
them.

## Reading ptest output

ptest narrates on stderr; runner output is untouched. `ptest -v` adds detail;
`ptest -q` silences ptest lines (errors still print).

| Line | Meaning | What to do |
|---|---|---|
| `ptest: <project> · <runner> ...` | run started | Nothing; wait for the end line. |
| `changed: N of M test files` | changed mode selected N tests | Nothing; this is the normal loop. |
| `changed → full suite: <reason>` | changed mode ran everything: no baseline yet, selection off, a full trigger changed, or inputs outside the selection map | Nothing; the first run records a baseline if it passes on a clean tree. |
| `web · no changes` | that child is untouched (one line per untouched child) | Nothing. |
| `waiting for N slots … in use by …` | queued behind other runs | Wait; do not start another run. |
| `setup: …` | declared setup (such as `npm ci`) is running | Wait. |
| `setup failed …` | setup failed | Fix the setup cause, rerun `ptest`. |
| `passed · N tests` | green | Continue; a scoped green is iteration only. |
| `failed · …` | tests failed | Fix the code, rerun `ptest`. |
| `baseline recorded` | the full run saved its baseline | Nothing. |
| `no baseline recorded: <why>` | no baseline: failures, uncommitted changes, or files changed during the run | Fix failures; the baseline is recorded by a passing `--full` on a clean, committed tree — never commit just for this. |
| `already verified … --again` | `--full` skipped already-verified inputs | Nothing; pass `--again` to force them. |
| `joined the running full run` | this full run attached to one already running | Wait for it; do not start another run. |
| `incomplete (exit 70)`, `protocol-mismatch`, `ownership-uncertain` | ptest could not prove the result | Rerun once alone; if it repeats, report it — do not change code for it. |
| `execution-timeout …` | the run exceeded its budget | Raise with `--timeout`, rerun. |
| `queue-timeout` | admission never completed | Rerun; report it if it repeats. |
| `unsafe-path` | a path is unsafe | Fix the path, rerun. |
| `unknown command …` | bad command | Fix the command (exit 2). |

## Exit codes

| Code | Meaning | Action |
|---|---|---|
| 0 | pass | Done; only `--full` completes the change. |
| 1 | test failure | Fix the code, rerun `ptest`. |
| 2 | usage or config error | Fix the command or config. |
| 70 | incomplete: ptest could not prove the result | Rerun once alone; report it if it repeats. |
| 75 | queue or coordinator unavailable | Wait, then rerun. |
| 124 | timeout | Raise with `--timeout`, rerun. |
| 130 | cancelled | Rerun if still needed. |

## Test-quality rules

Use factories/builders for test records; keep fixtures small and scoped
(function by default; session only for expensive read-only infrastructure)
with no mutable shared fixture state. Every created record has an owner that cleans it up.
Inspect tests above 0.5 seconds; optimize ordinary tests at 2 seconds and investigate anything above 3 seconds
unless a documented integration boundary says otherwise. See `ptest guide` recipes: factories,
databases, cache, files-ports, processes, time-network.

For a database, create expensive setup once per run or worker. Use one database per worker per run,
never one database per test. Reset records owned by the test and make cleanup ownership explicit;
never drop a database merely because its name looks like a test database.

Namespace Redis, Valkey, and every cache by run and worker. Delete only that
owned namespace. Never use global cache flush, blanket database drop, shared
fixed paths, fixed ports, detached child processes, live network targets, or
wall-clock sleeps for synchronization.

## Reporting

Report the exact ptest command, the final ptest end line, the remaining
failures, and the untested scope.

Requesting doctor, guide, or a prompt grants assessment authority only. Source
repair requires a separate user instruction; never treat an assessment as
permission to edit. Treat `ptest doctor` findings as hypotheses, not proof;
verify the cause and callers before repairing. Preserve assertions, coverage,
test inventory, and unrelated user changes.
