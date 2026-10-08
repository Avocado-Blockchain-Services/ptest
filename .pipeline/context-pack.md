# Context pack — ptest test-policy (2026-10-08)

Worktree: /home/ingmar/worktrees/ptest/cc-test-policy/ptest  (branch feature/test-policy, off local main bbf1260; nothing unpushed inherited)
Ledger/pack are tracked paths marked `git update-index --skip-worktree`; never stage .pipeline/ (it is not gitignored). Stage owned files by explicit path.

## Runner and test commands (ptest repo)
- runner: ptest (root .ptest.toml committed on main). Never `ptest init` in this worktree; `config-uncommitted` = stop and report.
- TDD loop: `ptest` (bare, from worktree root) or `ptest tests/ng/<file>.py`
- Final integrated gate (Verify phase only, once): `ptest --full`
- Never call pytest directly, never add --workers/--timeout. No installCmd (ptest installs deps). No coverageCommand/gate in ptest mode.
- envFiles: none. localOnlyPaths: none. No DB migrations.
- After source changes: `graphify update .` in the worktree.

## Precedent to imitate
src/ptest/agent_rules.py — managed-file install with hash-tracked refresh (`_guide()`, `_block()`, `_managed_state()`, `_PREVIOUS_GUIDE_SHA256S`, `_guide_kind`, `_refresh_targets`, `refresh`, `preview`, `apply`, `_rollback`, fd-based `_create_leaf`/`_replace` with symlink/size checks, `_MAX_FILE_BYTES=256KiB`). The policy file must follow exactly this pattern (bundled resource bytes, previous-version hash set, user-edited copy = never overwritten).

## Facts read from the tree
- docs/ptest-agent.md is byte-identical to src/ptest/resources/repository-agent-guide.md (repo dogfoods it); update both together.
- Guide is exactly 100 lines; cap asserted at tests/ng/test_agent_rules.py:180 (`<= 100`). Cap moves only by the lines the `## Writing tests` section adds (design fixes the number; T3 bumps it).
- Pre-change sha256 of repository-agent-guide.md (main bbf1260): 5ac1f26cabd7413c4e68456aae4aa6345c4678d6dca769855463d3e2d196d2d2 — add to `_PREVIOUS_GUIDE_SHA256S` with comment `# bbf1260: guide before the Writing tests section.` (test `test_every_shipped_guide_version_hashes_into_previous_set`).
- agent-guide.md (rendered by `ptest guide`, render.py:1476) is not installed into repos; no hash set needed.
- Managed block: markers `<!-- ptest-agent-rules:start -->` / `:end -->`; `_block(name)` = one reference line (AGENTS.md prose line, CLAUDE.md/GEMINI.md `@docs/ptest-agent.md`); `_managed_state` rejects anything not byte-equal → must accept exactly two variants (base, base+policy line).
- worktree.py AGENT_RULE_FILES / MANAGED_MARKER / _MARKED_RULE_FILES; uninstall.py `_GUIDE_REL`, `_is_managed_guide`, `_decide_guide`, `_decide_block`, `_guidance_entries`.
- CLI: cli.py `init` parser (~L290-420, `--agents`, `--dry-run`), `rules` parser (~L448: currently only `--apply`), `_init_agents` prompt (~L2947, TTY + not json + not explicit), doctor online flow (~L2192 render_recommendations / render_agent_assessment) and offline `_doctor_static_output` (~L2270).
- Doctor rendering: render.py `render_agent_assessment` (L1008), `render_doctor` (L1348); recommendations.py `render_recommendations` (L762). JSON schemas frozen: docs/schemas/v1/doctor.json, docs/schemas/v1/init.json — do not change.
- pyproject.toml [tool.setuptools.package-data] lists resources explicitly (recipes/*.md covered by glob; a new resources/test-policy.md needs an entry).
- Eval kit: evals/agent-usage/scenarios.toml (+ scripts/agent_eval.py scorer; tests in tests/ng/test_agent_eval.py).
- Changelog: docs/changelog.md `## Unreleased — ptest NG` (L434). README.md.

## Files this feature touches (owner task)
- T1: src/ptest/resources/repository-agent-guide.md, docs/ptest-agent.md, src/ptest/resources/agent-guide.md, src/ptest/resources/recipes/tests.md (new), src/ptest/checklist.py, evals/agent-usage/scenarios.toml, scripts/agent_eval.py, tests/ng/test_checklist.py, tests/ng/test_agent_eval.py, tests/ng/test_writing_tests_guidance.py (new), README.md, docs/changelog.md
- T2: src/ptest/test_policy_facts.py (new), src/ptest/render.py, src/ptest/recommendations.py, tests/ng/test_test_policy_facts.py (new), tests/ng/test_recommendations.py
- T3: src/ptest/agent_rules.py, src/ptest/resources/test-policy.md (new; installed as docs/ptest-test-policy.md), src/ptest/worktree.py, src/ptest/uninstall.py, pyproject.toml, tests/ng/test_agent_rules.py, tests/ng/test_uninstall.py, tests/ng/test_worktree.py
- T4: src/ptest/cli.py, src/ptest/init_render.py, tests/ng/test_cli.py, tests/ng/test_init.py, tests/ng/test_init_render.py, tests/ng/test_doctor_test_policy_cli.py (new)
