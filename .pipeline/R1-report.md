# R1 report — review fix round 1 (feature/worktree-safe-config-R1 @ 1d9ef59)

Base: `feature/worktree-safe-config` @ fcdfe1e. Worktree: `ptest-R1`.
Strict TDD: every behavior change got a failing test first (failures observed
pre-fix, green post-fix). Verify run: `ptest --workers 2 --queue-timeout 1800`
over test_config/worktree/init/help/agent_rules/cli/doctor → **926 passed**.

## 1. [MEDIUM] config-uncommitted wording covers the stale-branch case
- `src/ptest/config.py:187-193` — message now ends "...commit {name} on the
  base branch, or, if it is already committed there, to update this branch
  from it. Do not run ptest init here." Detection untouched.
- Consistent reword in `src/ptest/help.py` (init notes + agents stop rule),
  `README.md:254-261`, `repository-agent-guide.md:51` (same line, still 100
  lines total), `docs/changelog.md:5-8`.
- Guide bytes changed → added the fcdfe1e guide sha256 to
  `_PREVIOUS_GUIDE_SHA256S` in `src/ptest/agent_rules.py` (required by
  `test_every_shipped_guide_version_hashes_into_previous_set`).
- Tests: updated exact-message pin in `tests/ng/test_config.py`; new
  `test_resolve_config_branch_predates_committed_config_mentions_update`
  (worktree added before main commits `.ptest.toml` → code
  `config-uncommitted`, message contains "update this branch"). Failed
  pre-fix, passes post-fix.

## 2. [LOW] submodule children excluded from uncommitted warnings
- `src/ptest/worktree.py` — new `_is_submodule_checkout` (lstat on
  `<root>/<child>/.git`, true only for file/dir, no symlink following);
  `_uncommitted_inner` skips such children. `main_config`/`_main_children`
  untouched.
- Test: `test_uncommitted_config_files_skips_submodule_children` (child dir
  with a `.git` file → only root `.ptest.toml` reported). Failed pre-fix
  (`api/.ptest.toml` wrongly listed), passes post-fix.

## 3. [LOW] changelog accuracy
- `docs/changelog.md:16-18` — run warning now quoted as
  (`new worktrees won't have it`); doctor keeps the
  commit-on-the-base-branch fix. No behavior change.

## 4. [LOW] stopgap cleanup note
- `README.md` worktrees section + `--from-main` stopgap warning in
  `src/ptest/config.py:1461-1465`: "Delete the copied file(s) before
  updating the branch." Existing `in`-match pins unaffected.

## 5. [NOTE] drift guard
- `test_agent_rule_lists_match_agent_rules_module` in
  `tests/ng/test_worktree.py`: asserts `worktree.MANAGED_MARKER ==
  agent_rules._MARKER_START` and `AGENT_RULE_FILES` matches
  `_GUIDE_PATH + _AGENT_FILES + _PROVIDER_SKILLS` values as a set
  (order differs, content must not). Passes — no drift found.

## Notes
- `src/ptest/doctor.py:697` remediation ("commit it on the base branch")
  intentionally untouched: it covers locally-uncommitted files, not the
  stale-branch detection case, and the file is out of scope.
- Nothing pushed/merged; main untouched.
