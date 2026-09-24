# `ptest uninstall` — requirements

Status: requested by the user on 2026-09-24 ("I need an additional … `ptest uninstall` … it should clean up"). This is the
authoritative spec; follow `secure-by-spec`, since it deletes files. Base: `main` 4bee767.

## Purpose

`ptest uninstall` cleanly reverses what ptest set up in a repository: everything `ptest init`, `ptest doctor` and ptest runs created
for this checkout. The repository must end up as it was before, and anything the user wrote or edited must never be lost.
An optional `--self` also removes the local ptest installation.

## Scope: what is removed (repository)

Anchor at the Git root, exactly as init does, whether run from the root or from a subdirectory. For a monorepo, handle the root
and every child.

1. **Config:** the root `.ptest.toml` (v1 or the v2 dispatcher) and every child `.ptest.toml` it declares or that ptest discovers
   as a ptest config. Configs belong to ptest even when the user tuned them, so they are always removed; the plan says so.
2. **Guidance** (`src/ptest/agent_rules.py` knows these; reuse its managed-state detection and never duplicate it):
   - `docs/ptest-agent.md`: remove it only when it is ptest-managed (the current bytes or a known previous hash). Otherwise
     keep it and report "kept (edited)".
   - The `<!-- ptest-agent-rules:start -->` … `<!-- ptest-agent-rules:end -->` block in `AGENTS.md` and `CLAUDE.md`:
     - remove exactly the block, plus the separating blank line init added, leaving every other byte identical
       (verify against init's insertion format);
     - if the file consisted only of that block (init created it), remove the file;
     - if the markers are unbalanced or duplicated, touch nothing in that file and report it.
   - The skills: `.claude/skills/ptest/SKILL.md`, `.agents/skills/ptest/SKILL.md`, `.opencode/skills/ptest/SKILL.md`,
     `.gemini/skills/ptest/SKILL.md`, and the legacy `.codex/skills/ptest/SKILL.md`. Remove each only when ptest-managed,
     then remove the directories that are left empty (`skills/ptest`, `skills`, and the tool dir itself). Never remove a
     non-empty directory, or anything else in it. For example `.opencode/package.json` and `node_modules` belong to OpenCode.
3. **Reports:** `recommendations.md` only when it carries a valid ptest-recommendations marker and its recorded hash matches
   (unedited). An edited report is kept and reported.
4. **Private runtime state for THIS checkout only:** any repo-local ptest state, and this checkout's entries in the
   machine-level domain (history, baselines, setup fingerprints, review-model cache, admissions or ledgers keyed to this
   checkout or project). **Never** touch another checkout's or project's state, or shared machine-wide records other runs
   depend on. If a ptest run for this checkout is active, refuse with a clear reason. Find the real state locations in the
   code (`platform.domain_paths`, `storage`, `history`, `scheduler`, and `cli`'s review-model cache); do not guess.

## `--self`: remove the local installation

Remove the ptest installation that `install.sh` created: the install root (default `~/.local/ptest`, or the configured
destination) with its `.ptest-bundles/*`, `ptest` symlink, and `complete.json` markers. Also remove a `ptest` symlink on
PATH (for example `~/.local/bin/ptest`) **only** when it resolves into that install root. Never touch a system Python, a PATH
entry the user wrote, or anything outside the install root. If the running `ptest` lives inside the root being removed, make
sure the removal still completes, and print a final line saying ptest was uninstalled. `--self` can run on its own, outside any
repository, or together with the repository cleanup.

## UX and safety

- **Plan first:** compute the full plan and show it in the new plain output style (terminal width, grouped by action:
  `remove`, `kept (edited)`, `skipped`). On a TTY, ask once: `Remove these? [y/N]`. Non-interactive runs require `--yes`, and
  without it show the plan and exit non-zero with a hint. `--dry-run` shows the plan and changes nothing. `--json` gives the plan
  and result as an additive public document, following the existing contract, schema and drift-guard patterns.
- **Never follow symlinks:**
  - delete only regular files inside the repository root (or inside the install root for `--self`), using the
    no-follow and open-by-fd helpers in `src/ptest/files.py`;
  - a symlinked `.ptest.toml`, skill, or `docs` dir is reported and left alone;
  - removing a block must never write through a symlink.
- **Block removal:** rewrite atomically (temp file plus rename in the same directory), keep the file mode, and fail closed if
  the file changes between planning and rewriting.
- **Idempotent:** a second `ptest uninstall` reports "nothing to remove" and exits 0.
- **Exit codes** follow ptest's conventions: 0 on success or nothing to do; non-zero for refused, unsafe, or missing `--yes`.
- **`--help` and README** document the command. Uninstall must not leave any doctor or init state that makes a later
  `ptest init` behave differently: init, then uninstall, then init must give a clean first init.

## Tests (strict TDD; tests through ptest only)

- **Round trip:** init (monorepo with every guidance target, and a doctor report), then uninstall. The tree must match the
  pre-init snapshot byte-for-byte, including `AGENTS.md`/`CLAUDE.md` content that existed before init.
- **Keep edited files:** an edited guide, skill, or report is kept and reported; an edited config is still removed.
- **Unmanaged or foreign files:**
  - unbalanced markers are left alone;
  - `.opencode/package.json` and `node_modules` survive;
  - a non-empty skills dir keeps its other files.
- **Symlinks:** at `.ptest.toml`, at a skill, and at `docs/`; also an `AGENTS.md` that is a symlink.
- **Consent:** TTY decline or EOF means nothing is changed; non-TTY without `--yes` exits non-zero and changes nothing;
  `--dry-run` changes nothing.
- **State:** this checkout's private state is removed while a second checkout's survives. An active run refuses.
- **`--self`:** exercised on a fixture install root (an install.sh-shaped layout in tmp). A PATH symlink pointing elsewhere is
  kept. The install root is never removed when it doesn't look like an install.sh layout.
- **Idempotence:** a second run reports nothing to remove.
- **Subdirectory:** running from a subdirectory anchors at the Git root.

## Constraints

- Keep it simple: one new module (for example `src/ptest/uninstall.py`) plus CLI wiring. Reuse `agent_rules`, `files`,
  `config` and `monorepo` helpers rather than copying them.
- Never delete outside the repo root or the install root. Never launch real claude/codex/opencode.
- No push or merge; the controller validates on a real persea worktree and then releases.
