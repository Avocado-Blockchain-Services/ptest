# ptest rules for coding agents

Run every test command through `ptest` from the repository root (the directory
holding the root `.ptest.toml`). Never invoke pytest, vitest, `npm test`,
`go test`, or `cargo test` directly.

## The loop

| Situation | Command |
|---|---|
| After each edit | bare `ptest` with NO project name: it runs only the tests your change reaches |
| Changed tests under one folder | `ptest <folder>`, e.g. `ptest <project>/tests` |
| One test file (always runs it) | `ptest <file>`, e.g. `ptest <project>/tests/test_x.py` |
| All tests under one folder | `ptest --full <folder>` |
| Before handoff or merge | bring the base branch in first, then `ptest --full`; fix and rerun until green |

A scoped or changed green is iteration only; only `ptest --full` completes the
change. Rerun it only after a change (`--again` forces one). Fast-forward the
green tip and never rerun for the merge (`already verified … in <checkout>`).
Keep notes and reports outside the repo, or in declared gitignored outputs.
Run these commands exactly as shown: never add `--workers`, `--timeout` or `--queue-timeout` yourself or copy them from older notes; ptest sizes workers from the project and the machine-wide queue shares the load, so waiting is normal.

## Monorepo

Always run from the monorepo root. Prefix scopes with the owning child, such
as `ptest <project>/tests/test_example.py`. Never cd into a child to run tests.
Child `.ptest.toml` files remain authoritative; never copy, merge, or rewrite them.

## Reading ptest output

ptest narrates on stderr; runner output is untouched. `ptest -v` adds detail; `ptest -q` silences ptest lines (errors still print).

| Line | Meaning | What to do |
|---|---|---|
| `ptest: <project> · <runner> ...` | run started | Nothing; wait for the end line. |
| `changed: <path> (+N files) → N of M test files (D direct · V via importers)` | changed mode selected the tests your change reaches | Nothing; this is the normal loop. |
| `changed → full suite: <reason>` | this run IS the full suite: a full trigger changed, selection is off, inputs outside the import graph, or the affected set is too large | Nothing extra; do not start `ptest --full` because of this line. |
| `changed: <path> → vitest changed delegation` | that child delegated its changed set to vitest | Nothing. |
| `changed: <path> → no tests affected` / `no changes vs <label> — nothing to test` | the change reaches no tests / nothing changed anywhere (exit 0) | Nothing; `ptest --full` runs everything if needed. |
| `no changes under <folder> — nothing to test` | the change reaches no tests under that folder (exit 0) | Nothing; `ptest --full <folder>` runs everything there. |
| `<project> · no changes` | that child is untouched (one line per untouched child) | Nothing. |
| `· next: ptest --full before handoff` | changed-mode green; the integrated gate is still needed | Keep iterating with `ptest`; run `ptest --full` once only when you are done. |
| `waiting for N slots …` / `waiting: <reason>` | queued behind other runs; the reason names the real blocker | Wait; do not start another run. |
| `setup failed …` | setup failed | Fix the setup cause, rerun `ptest`. |
| `passed · N tests` | green | Continue; a scoped green is iteration only. |
| `failed · …` | tests failed | Fix the code under test, then rerun `ptest`; never weaken, skip or delete tests or assertions to get green. |
| `joined the running full run` / `already verified at …` / `full gate runs: <why>` | attached to a running full run / this exact tree already passed (here or in another checkout) / why it must run | Wait / nothing to do / follow the named fix. |
| `incomplete (exit 70)`, `protocol-mismatch` | ptest could not prove the result | Rerun once alone; if it repeats, report it — do not change code for it. |
| `ownership-uncertain` | ptest could not prove that a run's processes are gone (a run started in another sandbox is judged by its lease lock only, which cannot see processes that run left behind) | Rerun once alone; if it repeats, find the stuck run with `ptest status --json` and run `ptest release <run_id>` (it refuses while anything is alive); if it refuses, report it — do not change code for it. |
| `execution-timeout …` / `queue-timeout` | the run exceeded its time limit / waited out the queue limit | Do what the line says (rerun once unchanged, or report a fixed limit); report a repeat: the time limit is `[runner] timeout` / `full_timeout` in `.ptest.toml`; for the queue, name what the waiting line says holds it. |
| `config-uncommitted: …` | this linked git worktree lacks the committed `.ptest.toml` that the main checkout has | Stop and tell the user to commit `.ptest.toml` on the base branch, or to update this branch if it is already committed there; never run `ptest init` here. |
| `unsafe-path` / `unknown command …` | a path is unsafe / bad command | Fix the path or command (exit 2 for a bad command), rerun. |
| `update available: X … run: ptest update` / `ptest X can improve this config … run ptest doctor --fix` | a newer ptest exists / this config predates it | Run `ptest update` (running jobs keep their version) / run `ptest doctor --fix`; then continue. If it says `installed from source`, tell the user. |

## Exit codes

| Code | Meaning | Action |
|---|---|---|
| 0 | pass | Done; only `--full` completes the change. |
| 1 | test failure | Fix the code under test, then rerun `ptest`; never weaken, skip or delete tests or assertions to get green. |
| 2 | usage or config error | Fix the command or config. |
| 70 | incomplete: ptest could not prove the result | Rerun once alone; report it if it repeats. |
| 75 | queue or coordinator unavailable | Rerun once unchanged; report a repeat. |
| 124 | timeout | Rerun once unchanged; report a repeat. |
| 130 | cancelled | Rerun if still needed. |

## Test-quality rules

Use factories/builders for test records; keep fixtures small and scoped
(function by default; session only for expensive read-only infrastructure)
with no mutable shared fixture state. Every created record has an owner that cleans it up.
Inspect tests above 0.5 seconds; optimize ordinary tests at 2 seconds and investigate anything above 3 seconds
unless a documented integration boundary says otherwise. See `ptest guide` recipes: factories,
databases, cache, files-ports, processes, time-network.

Reuse expensive server/schema setup once per run or worker. A fresh SQLite
database or mutable instance per test/use can provide clear ownership. Namespace
shared database records and external caches by overlapping owners; a fresh
per-test/per-use instance can own local cache state. Delete only owned records;
never globally flush caches or drop a database by name alone. Avoid shared
fixed paths, fixed ports, detached children, live network targets, and
wall-clock sleeps for synchronization.

## Reporting

`ptest doctor` asks consent, then makes one initial call per model-assessed item
and one bounded verification for each valid reply, including `OK`. It uses
requested models Codex `gpt-6-sol` and Claude `opus` by default; overrides are
explicit. `ptest doctor --offline` is static and sends nothing. Reviews cover
cited reachable units only; omitted decisive callers or failure paths remain
unknown. Timing, selection, and parallel-execution items use ptest's own facts.

Untracked config: `ptest: .ptest.toml is not committed` — tell the user; do not commit it yourself unless asked.

Requesting doctor, guide, or a prompt grants assessment authority only. Source
repair requires a separate user instruction; never treat an assessment as
permission to edit. Treat findings as hypotheses, not proof; verify the cause
and callers before repairing. Preserve assertions, test inventory,
and unrelated user changes. Report the exact ptest command, the final ptest
end line, the remaining failures, and the untested scope.
