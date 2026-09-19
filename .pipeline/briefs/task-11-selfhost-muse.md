# T11 self-hosting gate repair

Work only in `/home/ingmar/worktrees/ptest/cx-ng-product/task-11-selfhost` on branch `cx-ng-product-task-11-selfhost`. You are not alone in the codebase. Do not touch other worktrees or revert user/controller changes. Read `AGENTS.md`, `.pipeline/context.md`, `docs/plans/2026-09-17-ptest-ng.md` Task 11 and 12, the relevant `config.py`, `cli.py`, and existing init/CLI tests before action.

This is a clean, local-only ptest NG repository. Do not add any legacy configuration import, global fallback, cloud/remote/backend/model/API dependency, alias, compatibility route, or silent unscheduled behavior. Do not edit production source, adapters, scheduler, contracts, dependencies, lock files, or tests. Do not push, merge main, deploy, publish, install over the live CLI, run raw test runners, or call cloud tools.

## Goal

Repair the missing Task 11 self-hosting prerequisite. The candidate `./.venv/bin/ptest where` currently reports `initialized: False` solely because this repository has no native `.ptest.toml`. Candidate `./.venv/bin/ptest init --dry-run --json` positively identifies the local `pytest` native profile and its exact canonical fields. Establish one committed, canonical root `.ptest.toml` using the fresh config contract, then prove the candidate `./.venv/bin/ptest` can execute a scoped NG test. This is the intended per-repository initialization path, not a legacy bridge.

## Ownership

Only root `.ptest.toml`, `.pipeline/out/task-11-selfhost.json`, and this task brief may change. Use `apply_patch` for every file edit. The report must be valid JSON and record raw commands, cwd, exit status, relevant output, the config byte content/digest, and any setup/network behavior observed. Commit only these owned files.

## Required procedure

1. Run candidate `./.venv/bin/ptest init --dry-run --json`; retain its fresh generated config summary. Do not write with `ptest init`; reproduce its canonical serialized config via `apply_patch` using the preview's exact fresh `project_id`, pyproject-derived Pytest launcher/setup, and defaults from `config._serialize_fresh`.
2. Before writing, show that candidate `./.venv/bin/ptest where` is uninitialized. After writing, run `./.venv/bin/ptest where --json` and assert `initialized: true`, Pytest runner, no global/legacy config provenance.
3. Prove candidate self-hosting with one scoped fast NG test module selected from the existing suite, through `./.venv/bin/ptest` only. It must use the candidate source in this worktree; do not fall back to `scripts/ptest-bootstrap`, `pytest`, `uv run pytest`, or a raw runner. If it needs the configured local `uv sync`, let candidate ptest perform declared setup and record the outcome; do not manually install dependencies. Preserve literal output and nonzero evidence if it fails.
4. Re-read every edited byte; report at least one thing checked during that reread. Inspect diff/ownership, run `git diff --check`, and commit only if the candidate `where` and self-hosted scoped run pass. If candidate self-hosting cannot pass, do not claim completion: report exact blocker, make no workaround/legacy fallback and do not commit speculative config.

Return a concise implementation/verification report with commit SHA or exact blocker.
