# ptest NG product workflow

This branch is the spec-gated, local-first ptest NG workflow. Read
`.pipeline/context.md` for scope and stage boundaries. Existing cloud code is a
migration reference, not the target design. The separate `cx-ng-foundation`
worktree is provisional; do not modify it or assume its decisions are approved.

- All tests must go through `ptest`, never a raw test runner. Use scoped runs
  during TDD and one final integrated `ptest --full` after implementation.
- Specification-only work must not run tests or install project dependencies.
- Use `uv`, never pip. Never share `.venv` or `node_modules` across worktrees.
- Worktrees live under `/home/ingmar/worktrees/ptest/<tool>-<slug>/<slot>`.
- Read and follow the `secure-by-spec` skill before implementation; every audit
  follows `audit-spec`. Include negative contracts and regression evidence.
- Preserve literal argv, test output/exit status, coverage gates, and user files.
- Product runtime must not require model APIs, cloud services, or a specific TUI.
- Keep mutable resources isolated by repository/worktree, run, and worker where
  applicable. Never delete resources owned by another run.
- Maintain a single source of truth, explicit public contracts, and small modules.
- After source changes run `graphify update .` in the changed checkout. Never run
  semantic extraction that sends source to another provider without approval.
- Each parallel task owns declared files in its own worktree; do not revert others.
- Commit only inside your assigned worktree. Never merge to main, push, publish,
  deploy, or replace the installed CLI. No real database migrations or cloud ops.
- The orchestrator integrates approved task commits onto the chain branch only.

Model routing and review gates are recorded in `.pipeline/context.md`.

## Pipeline history cutoff

For implementation work, do not read or dump pipeline history created before
2026-09-21. This includes older `.pipeline/context.md` sections, historical
`.pipeline/briefs/`, `.pipeline/out/`, progress ledgers, transcripts, and
design/review artifacts. Use only the current task brief and pipeline records
dated 2026-09-21 or later, unless the user explicitly asks for older history.
Do not invoke broad history/graph searches that pull those records into
context. Inspect the current source, tests, and task-owned files directly.
