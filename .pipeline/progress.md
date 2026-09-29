# pipeline — Implement the brief in /tmp/claude-1000/-home-ingmar-code-tools-ptest/e8898983-6043-4619-a933-8d5abd5c42c4/scratchpad/update-check-brief.md — ptest checks for a newer release at startup (cached 24h, 2s timeout, silent on any error), asks a human at a terminal to update then re-execs, prints one `ptest update` line for agents/non-TTY, and adds a SHA-256-verified `ptest update` command that reuses the bundled installer. Keep all code in ONE task; docs in a second independent task.

- 2026-09-29 setup: worktree /home/ingmar/worktrees/ptest/cc-update-check/ptest on feature/update-check off local main (985d43e, Release 0.3.7); no unpushed commits vs origin/main; no .env; `uv sync --locked --extra test` prewarmed.
- Tasks: T1 code (update.py + cli/help/contracts/schema + tests), T2 docs (README, installation, changelog, agent guide).
- 2026-09-29 verified: scoped ptest on tests/ng/test_get_sh.py passed (8 tests) and printed the coverage table.
