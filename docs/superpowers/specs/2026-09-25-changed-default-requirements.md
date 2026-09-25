# `ptest --changed` as the default loop — requirements

Status: requested by the user on 2026-09-25. Goal: once a project works, the everyday command is `ptest --changed` — run only
what the change touches — with `ptest --full` once at the end. A real agent in persea reported it is "nowhere instructed" to
use --changed, and `ptest --changed` at a monorepo root fails today ("a monorepo scope is required").

## A. `--changed` works at a monorepo root (cli.py monorepo branch, monorepo.py)
1. Root `ptest --changed` runs each declared child in CHANGED mode against the same base (`--base` passes through).
   A child whose directory has no changed files (vs base, including uncommitted and untracked) is skipped with one line
   `ptest: web · no changes`. A child with changes runs with ptest's normal changed-mode planning (pytest selection when
   qualified, otherwise its full suite with the reason).
2. Vitest child with changes: if simple, use vitest's own `--changed <base>` so only affected test files run; otherwise run
   its full suite and say why. Record the choice in design notes.
3. One end line per child plus the existing total line; exit = first nonzero.
4. Changes outside every child (root files) that are full triggers for a child (lockfiles, .ptest.toml) make that child run.
Twins: root --changed with changes only in api → web skipped, api runs; changes in both; no changes at all → every child
"no changes", exit 0; --base passthrough; vitest --changed argv (or full-suite fallback with reason).

## B. `--changed` explains its decision (operations.py, progress.py)
1. Start line in changed mode names the plan: `… · changed: 12 of 812 tests (3 files changed)` or
   `… · changed → full suite: <reason>` where reason is plain words for each existing selection reason code:
   no baseline yet (this run records one if it passes on a clean tree) · selection is off in <cfg> (ptest doctor --fix) ·
   <file> is a full trigger · <file> is outside the selection map · baseline is not an ancestor of HEAD · policy changed.
2. After a full run (--full or changed→full) that did NOT record a baseline, the end line says why in plain words:
   `no baseline recorded: 2 failures` / `…: uncommitted changes` / `…: files changed during the run` / `…: incomplete results`.
   When it did record one: `baseline recorded`.
Twins for each reason and for both end-line notes; -q suppresses them like other status lines.

## C. init offers `--changed` setup + agent guidance defaults to --changed (init/cli init path, agent_rules resources)
1. After the smoke step, for each pytest project whose environment has the frozen pytest-cov/coverage pair, init asks once:
   `Set up ptest --changed for api? Adds coverage to test runs; needs one full run as a baseline. [now/later/no] (default: later)`
   - now: write `--cov`/`--cov-report` + the `[selection]` draft (REUSE doctor_fix's planner — no duplicate logic), then run
     `ptest --full` for that project immediately (normal run output) so the baseline is recorded;
   - later: write the same config; the baseline is recorded on the first passing --full/--changed on a clean tree; init says so;
   - no: leave selection off.
   Non-interactive: `ptest init --changed-setup now|later|no` (default when non-interactive and no flag: later). Projects
   without pytest-cov: one line `api: ptest --changed needs pytest-cov (add it to the test deps)`; vitest: no question.
   --dry-run shows what would be written, runs nothing. Existing configs: never rewritten by init (point to doctor --fix).
2. Shipped agent guide (src/ptest/resources/repository-agent-guide.md) and skill template: the default loop is
   `ptest --changed` after each edit; a scoped path to target one test; `ptest --full` once after the integrated change.
   Mention that the first --changed may run everything to record a baseline. Keep the guide within its line cap. Register the
   previous guide/skill bytes in `_PREVIOUS_GUIDE_SHA256S` (old installs must stay recognised). README/help "Getting started"
   show `ptest --changed`.
Twins: now/later/no/Enter-default; non-TTY flag values; no-pytest-cov line; vitest skipped; dry-run; guide text + hash
registration round trip (init → edit nothing → uninstall recognises old and new bytes).

## Constraints
Strict TDD, tests through ptest only, keep it simple and reuse existing helpers; public schemas additive only.
