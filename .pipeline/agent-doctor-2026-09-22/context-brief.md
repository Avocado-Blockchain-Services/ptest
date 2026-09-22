# Bounded current-source context task

Return a concise context pack in the required JSON. Read-only, no source edits,
no tests, no dependencies, no children. At most 12 focused source reads and one
file-list query; do not load unrelated skills/pipeline docs. User prohibits ALL
pipeline/history before 2026-09-22; do not read .pipeline/context.md, git log,
blame, graph queries, personal memories, or earlier task outputs. Current repo
AGENTS.md applies with this direct cutoff override. Commit only in this assigned
worktree if asked (not asked here); never push, merge to main/dev, deploy, install
globally, run cloud commands, Terraform, or live DB migrations.

Goal: ptest doctor invokes installed Claude/Codex/OpenCode CLI for a read-only
per-project canonical checklist assessment; init shares same workflow. Agent
consent separate from guidance installation, no implicit installs/tests/repairs.
Report recommendations.md contains exact evidence, concrete repair advice and
self-verification: focused failing regression, root ptest command/prereqs/expected
result, actual command/cwd/exit/output evidence, final ptest --full, unresolved stays
unverified. Checklist percent not coverage/safety; unknown not satisfied, justified
N/A excluded; critical blockers override upbeat headline. Current pytest serial
limitation must remain honest. Progress/elapsed/provider/project and cancellation,
branded init wordmark+version+repo name+GitHub URL; JSON clean. Safe report writes.

Inspect current relevant sections in cli.py, doctor.py, checklist.py, render.py,
init_render.py, agent_rules.py and tests/ng plus CI current files. Identify exact
extension points, reusable safe-write precedent, scoped ptest commands, coverage
gate if any, migration N/A, and concrete disjoint file-ownership tasks. Confirm
current doctor JSON/probe compatibility constraints and dependency/parallel limits.
No design implementation yet. Return bounded JSON context, not broad audit.
