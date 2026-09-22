# Native skill discovery smoke — 2026-09-22

Status: complete. Both live low-effort agent checks passed. The final repaired
wheel was installed and verified against the worktree; final init/upgrade output
matches the exact skill and guide bytes loaded by those live agents.

Isolated evidence directory:
`/home/ingmar/worktrees/ptest/cx-init-onboarding/smoke-5Vh6ai`.
No original user repository or global ptest installation was modified.

## Installed candidate and deterministic CLI smoke

- `uv build --wheel --out-dir .../initial-dist`: exit 0.
- `uv venv .../venv`, then `uv pip install --python .../venv/bin/python
  .../initial-dist/ptest_ng-0.1.5-py3-none-any.whl`: exit 0. Version remains
  unreleased 0.1.5; this is a local development candidate, not a published release.
- `bash .../init-smoke.sh`: exit 0 for independent monorepo and standalone Git
  fixtures. Dry run made no file changes, init created both canonical skills,
  repeated init/JSON preserved file bytes and JSON envelope/payload key sets.
- Native Codex `skills/list` independently reported one enabled, repo-scoped
  ptest skill at each fixture's `.agents/skills/ptest/SKILL.md`, with no errors.
  Evidence: `monorepo.codex-catalog.json`, `standalone.codex-catalog.json`.

## Live agents (prompts supplied no skill path or body)

- Codex CLI 0.155.1; explicit `gpt-5.6-terra`, reasoning effort `low`, read-only
  sandbox. Transcript `codex.low.jsonl` records a successful shell read of
  `.agents/skills/ptest/SKILL.md` and `docs/ptest-agent.md`, followed by the correct
  commands `ptest api/tests/test_example.py` and `ptest --full`. Exit 0.
- Claude Code 2.1.278; `--model sonnet --effort low` (native startup resolved
  `claude-sonnet-5`), only Read/Skill tools allowed. `claude.low.jsonl` startup
  catalog includes `ptest`; actual tool-use record invokes `Skill` with
  `skill: ptest`. Successful result gives the same focused/full commands and
  identifies `.claude/skills/ptest/SKILL.md`. Exit 0.
- `live-smoke.sh` records exact flags/prompts; stdout/stderr, native versions and
  exit files are retained. No test, install, or source-edit authority was granted
  to either smoke agent.
- `cmp monorepo.before-agents.sha256 monorepo.after-agents.sha256`: exit 0.
  All non-Git fixture files are unchanged by the two live agent sessions.

## Final candidate verification

The `verified-dist` wheel includes all filesystem-safety repairs. Installed
source comparison passed; final standalone/monorepo fixtures, missing-child
attention, and legacy Claude upgrade were checked. Final skill and guide bytes
equal the exact files loaded above. The final full suite passed 2291 tests with
31 skips, and Terra approved evidence closure. See `2026-09-22-init-verification.md`.
