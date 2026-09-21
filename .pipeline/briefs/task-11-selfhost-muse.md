# T11 self-hosting gate repair

Work only in `/home/ingmar/worktrees/ptest/cx-ng-product/task-11-selfhost` on branch `cx-ng-product-task-11-selfhost`. You are not alone in the codebase. Do not touch other worktrees or revert user/controller changes. Read `AGENTS.md`, `.pipeline/context.md`, `docs/plans/2026-09-17-ptest-ng.md` Task 11 and 12, the relevant `config.py`, `cli.py`, and existing init/CLI tests before action.

This is a clean, local-only ptest NG repository. Do not add any legacy configuration import, global fallback, cloud/remote/backend/model/API dependency, alias, compatibility route, or silent unscheduled behavior. Do not edit scheduler, contracts, dependencies, lock files, or unrelated tests. Do not push, merge main, deploy, publish, install over the live CLI, run raw test runners, or call cloud tools.

## Explicit orchestrator reassignment

The 2026-09-19 Opus audits rejected the prior repair for incomplete guard-owned setup lifecycle, outcome truth, post-setup validation, stale-state handling, frame ordering and setup least privilege. The orchestrator explicitly authorizes the required Task 11 operations/setup repair, necessary obsolete-test corrections, and the stale pytest fixture-boundary documentation correction, superseding the prior no-production-source restriction. The exact expanded file set for this repair is: `.ptest.toml`, `src/ptest/operations.py`, `src/ptest/adapters/pytest.py`, `tests/ng/test_task_11d_pytest.py`, `tests/ng/test_cli.py`, `tests/ng/fixtures/pytest/README.md`, `.pipeline/out/task-11-selfhost.json`, and this brief. No other files may change.

## Goal

Repair Task 11 native Pytest self-hosting with a committed canonical root `.ptest.toml` and a guard-owned setup lifecycle. Declared setup must run only when required paths are missing or its private per-checkout tool/lock fingerprint is stale, must digest regular lock/manifest symlink targets while surfacing dangling or unsafe targets as setup state-unavailable, must be revalidated before execution, and must preserve truthful timeout, descendant, cancellation and nonzero outcomes. Candidate `./.venv/bin/ptest where --json` must report initialized local Pytest capability, and a scoped NG test must execute through candidate ptest. This is the intended per-repository initialization path, not a legacy bridge.

The final robustness boundary also requires bare setup-tool PATH scanning to skip unusable `OSError` entries and revalidation to convert `C.Problem`/`OSError` into a typed state-unavailable outcome without a traceback; direct regular-file PATH-entry and revalidation regressions cover both paths.

## Ownership

Only the exact reassigned file set above may change. Use `apply_patch` for every file edit. The report must be valid JSON and record raw commands, cwd, exit status, relevant output, the config byte content/digest, setup/network behavior, lifecycle evidence, and the final tree/commit. Commit only those owned files.

## Required procedure

1. Preserve the prior dry-run/before-state evidence and canonical root config; candidate `where --json` must report initialized local Pytest capability with only repository-local provenance.
2. Run focused TDD through candidate ptest for setup missing/present/stale/no-setup, post-setup missing path, timeout, lingering descendant, nonzero outcome, frame ordering and least-privilege environment; run changed operations/task11f/CLI modules and guard setup coverage.
3. Prove candidate self-hosting with one scoped fast NG test through `./.venv/bin/ptest` only. It must use candidate source in this worktree; never use bootstrap, raw runners, manual sync/install, or a fallback. Record whether declared setup ran or skipped and why.
4. Re-read every edited byte; report at least one thing checked during reread. Inspect diff/ownership, run `git diff --check`, refresh graphify, and commit only if candidate where/self-host and required scoped evidence pass. Record any full-gate limitation exactly without workaround or legacy fallback.

Return a concise implementation/verification report with commit SHA or exact blocker.
