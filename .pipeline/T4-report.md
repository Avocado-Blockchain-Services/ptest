# T4 report — agent guide row, help, README, changelog

- status: DONE (not blocked)
- taskWorktree: /home/ingmar/worktrees/ptest/cc-worktree-safe-config/ptest-T4
- taskBranch: feature/worktree-safe-config-T4
- commit: 299b3da ("T4: agent guide row, help, README, changelog for config-uncommitted")
- base: 890e1ee (design commit; T4 ran wave-1, no T1 dependency)

## TDD evidence

- Red: 4 new tests failed pre-implementation —
  `ptest --workers 2 --queue-timeout 1800` on the 4 new test IDs →
  `4 failed in 10.62s` (exit 1), log /tmp/t4-prefail.log.
- Green (final): `ptest --workers 2 --queue-timeout 1800
  tests/ng/test_agent_rules.py tests/ng/test_help.py` →
  `107 passed in 13.75s`, `ptest: passed · 107 tests · 18.6s` (exit 0),
  log /tmp/t4-post2.log. Re-run after the last source edit (guide
  paragraph break); no other scope run (design acceptance names exactly
  these two files).

## Diff (7 files, +106/-1; all inside the owned set)

- src/ptest/resources/repository-agent-guide.md: `config-uncommitted` table
  row placed directly before the `unsafe-path` row; "Untracked config"
  paragraph added in Reporting. Guide is now exactly 100 lines (test limit
  is `<= 100`) — future guide edits must shorten elsewhere first.
- src/ptest/agent_rules.py: `_PREVIOUS_GUIDE_SHA256S` gains
  `4fd66f8d…f9d` with the exact frozen comment
  `# 80392ca (0.3.3-0.3.5): guide before the config-uncommitted row.`
  (Pre-change bytes verified by `sha256sum` before editing.)
- src/ptest/help.py: `_INIT` syntax gains `[--from-main]`; notes cover the
  worktree refusal, verbatim stopgap (not combinable with
  `--runner`/`--child`), base-branch fix, and the files-to-commit listing.
  `_AGENTS` gains the one stop-rule sentence.
- README.md: new `## Worktrees and clones` section (commit files,
  `config-uncommitted`, `--from-main` stopgap + flag constraint, run
  warning, doctor line, init file listing). No separate init flag table
  exists in README, so the section documents the flag.
- docs/changelog.md: new top `## Unreleased` section, five bullets.
- tests/ng/test_agent_rules.py: `test_shipped_guide_documents_config_uncommitted`,
  `test_previous_hashes_cover_pre_config_uncommitted_guide`.
- tests/ng/test_help.py: `test_init_help_documents_from_main_stopgap`,
  `test_agents_help_documents_config_uncommitted_stop_rule`.

## Acceptance checklist (design §T4)

1. Guide row + reporting line: MET.
2. Hash entry + upgrade tests: MET (`test_every_shipped_guide_version_hashes_into_previous_set`
   and upgrade-in-place tests pass in the 107).
3. help.py init + agents: MET.
4. README section + changelog Unreleased: MET.
5. `test_user_facing_text_has_no_banned_terms` passes (in the 107); new
   assertions added: MET.
6. Scoped run green: MET.

## Integration notes (seams, not blockers)

- `docs/schemas/v1/init.json` NOT touched: the task brief listed it in my
  files, but the design (authoritative, §8 + D9) moved it to T1 (generated
  from `contracts.PUBLIC_SCHEMAS`). My changelog bullet mentions
  `commit_paths` in `init --json`; that payload is T1/T2's to produce.
- Wave-1 seam: `help.py` documents `ptest init --from-main`, which no
  `cli.py` in this worktree implements yet — that is T2's wave-2 delivery
  against the same frozen text. Same for the run-warning/doctor/finding
  sentences in README/changelog (T2/T3).
- Self-review (Pass 1) found and fixed one issue: the Reporting paragraph
  initially merged into the preceding paragraph (missing blank line).
- No migration generated. Never pushed, merged, or deployed. Worktree left
  clean at 299b3da.
