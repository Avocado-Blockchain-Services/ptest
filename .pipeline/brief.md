# Worktree-safe ptest config: committed config only, explicit guidance (no hidden fallback)

Repo: /home/ingmar/code/tools/ptest

## Problem

ptest's only project config is the in-checkout `.ptest.toml`
(`src/ptest/config.py` resolve_config/_find_config, bounded at the git worktree
root). `ptest init` writes it plus agent-rule files (`docs/ptest-agent.md`, the
managed ptest block in AGENTS.md/CLAUDE.md, `.agents/.claude/.gemini/.opencode`
`skills/ptest/SKILL.md`) but never tells anyone to commit them. New git worktrees
contain only committed files, so the config is missing there; ptest then advises
"run ptest init there" (`src/ptest/cli.py` ~3339 reroute path, ~1687 doctor
limitation). Agents obey, and every init mints a new random `project_id`
(`config.py` ~1051 `secrets.token_hex`) and a bare default config -> split
project identity, lost tuning, drift. Observed: 8 divergent `api/.ptest.toml`
variants across one repo's worktrees.

## Requirements

1. `ptest init` ends by telling the user to commit, listing the exact files it
   wrote or changed, and stating that worktrees and clones only get committed
   config.
2. In a checkout whose `.ptest.toml` (root or monorepo child) is untracked, every
   executing ptest run prints exactly one stderr warning line such as
   `ptest: .ptest.toml is not committed — new worktrees won't have it`
   (suppressed by -q like other ptest narration; never alters runner output or
   exit status). `ptest doctor` reports uncommitted ptest config/agent-rule
   files as a finding.
3. In a linked git worktree with no `.ptest.toml` where the main worktree has one
   at the same relative path: fail with a dedicated problem code
   `config-uncommitted` (human text and --json), naming the main checkout's
   config path, explaining worktrees only receive committed files, and
   instructing: ask the user to commit it on the base branch; do NOT run
   `ptest init` here. This replaces the "run ptest init" advice in that
   situation everywhere it appears (bare run, path reroute, doctor limitation,
   register/where/status as applicable).
4. `ptest init` refuses in such a worktree with the same code, unless
   `--from-main`, which copies the main checkout's config file(s) verbatim (same
   project_id; monorepo root manifest plus child configs) into the worktree and
   prints that the copy is a temporary stopgap that may go stale and that
   committing is the fix.
5. `docs/ptest-agent.md` (and whatever generates/ships it, e.g. agent_rules.py
   resources) gains a row: `config-uncommitted` -> stop, tell the user, never
   run ptest init. README/docs/help updated. JSON schemas (docs/schemas/v1) and
   contracts updated if problem codes are enumerated; changelog entry.

## Negative contracts

- Never execute with a config read from outside the current checkout (no fallback).
- Never modify the main checkout.
- Detect linked worktrees statically (.git file -> gitdir -> commondir) with the
  existing files.py ownership/no-symlink/bounded-read rules; no git subprocess
  where static detection suffices (tracked-check may use the existing _git helpers).
- Behavior unchanged for normal checkouts, worktrees whose config is committed,
  bare repos, and non-git directories.
- The untracked-warning check is cheap, best-effort, and never fails a run.

## Process

Follow AGENTS.md: spec-gated, secure-by-spec, tests only through ptest,
worktrees under /home/ingmar/worktrees/ptest/, final integrated `ptest --full`,
`graphify update .` after source changes. Then cut the next release (0.3.6)
and push, per the user's explicit instruction.
