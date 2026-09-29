# Context pack — worktree-safe ptest config

Spec: `.pipeline/brief.md` (copy of the authoritative brief). Worktree:
`/home/ingmar/worktrees/ptest/cc-worktree-safe-config/ptest`, branch
`feature/worktree-safe-config` off local `main` (73f0707, equal to origin/main).

## Install

`uv sync --locked --extra test` (already run; it is also the `[setup]` argv in
`.ptest.toml`, so ptest re-runs it when needed).

## Tests — ALWAYS through ptest, never raw pytest (AGENTS.md, docs/ptest-agent.md)

Run from the worktree root. `.ptest.toml` already carries
`--cov=ptest --cov-report=term`, so every ptest run prints a coverage table.
ptest admits workers per granted slot; the config caps at `workers = 8` and
ptest's coordinator serialises competing runs — do not start a second run
while one is queued ("waiting for N slots": wait).

- Scoped (TDD): `ptest --workers 2 --queue-timeout 1800 tests/ng/test_worktree.py tests/ng/test_config.py`
  (name only your task's test files). Without `--workers` ptest requests 8
  slots and can starve in the machine-wide queue; verified 2026-09-29:
  `ptest -v --workers 1 --queue-timeout 1500 tests/ng/test_files.py` -> 69 passed,
  coverage table printed (queue 1m51s).
- Coverage (the gate): same scoped command (`--workers 2`); read the `TOTAL`/per-module rows
  for your touched modules from the printed table.
- Full (orchestrator, once, after integration): `ptest --full --workers 2 --queue-timeout 3600`.
- Doc/spec-only edits must not run tests.

Coverage gate: **none** — no CI config exists (no .github/.gitlab-ci), and
pyproject has no `fail_under`. Do not regress coverage of touched modules.

Migrations: none (no database).

## Precedents

- Primary precedent (imitate): `src/ptest/config.py` — `_git_boundary`,
  `_candidate_config`, `_find_config`: static, no-subprocess, lstat-based,
  symlink-refusing detection returning `C.Problem` via `_problem(code, msg)`.
- Run-narration warning precedent: `src/ptest/cli.py::_warn_stale_guidance`
  (~2775, called ~3348): best-effort, try/except-all, `progress.emit`, -q
  suppressed, never alters output/exit.
- Bounded reads: `src/ptest/files.py::read_regular`, `create_exclusive`.
- Tracked check git helper: `src/ptest/source.py::_git` (ls-files usage ~855).

## Design contract (shared API — every task codes against this)

New module `src/ptest/worktree.py` (T1):
- `linked_worktree(root: Path) -> LinkedWorktree | None` — `root/.git` is a
  regular file `gitdir: <path>`; resolve gitdir, read `<gitdir>/commondir`
  (relative to gitdir), main worktree root = parent of common dir when the
  common dir is named `.git` (bare repo / other layouts -> None). Uses
  lstat, refuses symlinks, bounded reads (files.py rules). No git subprocess.
  `LinkedWorktree(root: Path, main_root: Path)` frozen dataclass.
- `main_config_paths(root: Path, relative: str = ".") -> tuple[Path, ...]` —
  if `root` is a linked worktree and `<main_root>/<relative>/.ptest.toml`
  exists as a regular file, return that path plus, when it is a monorepo root
  manifest, each declared child's `.ptest.toml` in the main checkout. Empty
  tuple otherwise. Read-only; never writes to the main checkout.
- `uncommitted_ptest_files(root: Path, *, include_agent_rules: bool) -> tuple[str, ...]`
  — repo-relative paths among `.ptest.toml` (root and monorepo children) and,
  when asked, agent-rule files (`docs/ptest-agent.md`, AGENTS.md, CLAUDE.md,
  `.agents|.claude|.gemini|.opencode/skills/ptest/SKILL.md`) that exist but
  are not tracked (`git ls-files --error-unmatch` or one `ls-files` call via
  existing helpers). Best-effort: any error -> `()`. Non-git -> `()`.

Problem code `config-uncommitted` (added to `contracts.REASON_CODES`, T2).
Message template (human + JSON `message`), exact wording owned by T2:
`config-uncommitted: this is a linked git worktree without .ptest.toml;
the main checkout has <main path>. Worktrees only receive committed files.
Ask the user to commit .ptest.toml on the base branch. Do not run ptest init
here.` Exit status 2 (config error), same as initialization-required.

`resolve_config` (T2): when no config is found and `worktree.main_config_paths`
is non-empty, return problem `config-uncommitted` instead of
`initialization-required`. Never load the main checkout's config.

`ptest init` (T2 core, T3 CLI): refuses with `config-uncommitted` in that
situation unless `InitOptions.from_main=True` (CLI `--from-main`), which
copies the main file(s) byte-for-byte (same project_id) via
`create_exclusive`, and returns an InitResult whose warnings say the copy is
a temporary stopgap that may go stale; committing is the fix.
`InitResult` gains `commit_paths: tuple[str, ...]` = files init wrote or
changed (config(s) + agent rule files). Human output ends with
"commit these files: …; worktrees and clones only get committed config".
`init --json` gains `"commit_paths": [..]` (schema `docs/schemas/v1/init.json`).

Run warning (T3): exactly one stderr line per executing run, -q suppressed:
`ptest: .ptest.toml is not committed — new worktrees won't have it`.
Doctor (T4): finding/limitation listing uncommitted ptest config/agent-rule
files; doctor/limitation text for config-uncommitted replaces "run ptest init".

## Files this feature touches

src/ptest/worktree.py (new), src/ptest/config.py, src/ptest/contracts.py,
src/ptest/cli.py, src/ptest/init_render.py, src/ptest/doctor.py,
src/ptest/help.py, src/ptest/agent_rules.py,
src/ptest/resources/repository-agent-guide.md, README.md, docs/changelog.md,
docs/schemas/v1/init.json; tests: tests/ng/test_worktree.py (new),
test_config.py, test_init.py, test_cli.py, test_init_render.py,
test_doctor.py, test_agent_rules.py, test_help.py, test_contracts.py.
`docs/ptest-agent.md` is NOT tracked in this repo (generated by init from the
resource); edit the resource and add the old resource's sha256 to
`agent_rules._PREVIOUS_GUIDE_SHA256S` so existing installs read as outdated.

After source changes: `graphify update .` in the worktree.

## Ownership addendum (triage)

- T1 (core) owns worktree.py, config.py, contracts.py and adds BOTH codes to
  contracts: `config-uncommitted` in REASON_CODES and `config.uncommitted` in
  FINDING_CODES (used by T4's doctor finding), plus InitOptions.from_main and
  InitResult.commit_paths. T2/T3/T4 code against the API above verbatim; the
  orchestrator integrates T1 first when sequencing is possible.
- T2 cli.py, T3 doctor.py + init_render.py, T4 docs/help/agent_rules/schema.
