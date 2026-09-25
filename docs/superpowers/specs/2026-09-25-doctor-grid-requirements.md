# Doctor: grid output (presentation only) — requirements

Status: requested by the user on 2026-09-25 with real persea output (below). Base: main fadb2ab (includes the Codex
"evidence-grounded doctor accuracy" work: src/ptest/review_protocol.py, agent_assessment.py).

## Scope
PRESENTATION ONLY (user: "only focus on the looks; the content of the doctor I am doing someplace else"). Do not change
review logic, validation, prompts, statuses, reason texts, finding texts, recommendations.md content or --json. Render the
existing data differently.

## G. Doctor terminal output as a grid (render/doctor/cli doctor path; offline and online)
Replace the per-project lists with one table: rows = checklist items (grouped: general, then a "parallel safety" group,
then Parallel execution), columns = projects. Above it, one facts line per project
(`api  pytest · runs ✓ · 4 workers · setup: uv sync --locked`). Header line: repo name · provider/model or offline ·
checks/calls · duration.
- Cells: `✓ ok` green, `✗ gap` red, `? unknown` yellow, `– n/a` dim; header bold; borders dim; box-drawing characters,
  ASCII fallback when stdout encoding is not UTF-8; no color with NO_COLOR/non-TTY/TERM=dumb/--json.
- Fit the terminal width (reuse terminal_width); if the table does not fit, stack one table per project.
- Below the table: **Gaps** — per gap: project · label, the finding, and `→` the fix (wrapped, hanging indent, never
  truncated). **Unknowns** — grouped by reason: identical reasons collapse into one line listing the checks
  (e.g. `api: Database setup reuse, Cache isolation, … — <the identical reason text, verbatim>`); distinct model reasons listed
  per check, wrapped. **Next** — the single most useful next command (e.g. `ptest doctor --fix`, `ptest --full`).
- recommendations.md and --json unchanged.
Twins: rendering snapshot for 2 projects (plain), color codes present only on TTY, ASCII fallback, narrow terminal stacks,
unknown-reason grouping, gaps never truncated, hostile labels/paths escaped.

## Constraints
Strict TDD; tests only via the worktree's .venv/bin/ptest, scoped files only (another workflow is refactoring tests/ng in
parallel — touch only the test files you need and prefer new test files); no --full (controller runs it); public schemas
additive only; keep it simple, reuse render/progress helpers.
