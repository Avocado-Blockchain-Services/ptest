# T2 report — CLI: --from-main, config-uncommitted surfacing, run warning, commit_paths, doctor mention

Worktree: `/home/ingmar/worktrees/ptest/cc-worktree-safe-config/ptest-T2`
Branch: `feature/worktree-safe-config-T2` (off `feature/worktree-safe-config` @ 5159175)
Commit: `3343450 T2: --from-main flag, config-uncommitted CLI surfacing, run warning, commit_paths, doctor mention`
TDD: 26 new tests written first → 16 failed / 10 passed pre-change (the 10 passed via T1 core) → implemented → all green.

## Files changed

- `src/ptest/cli.py` (+104): the only source file touched.
  - `ParsedArgs.from_main` + init parser `--from-main` flag (repeat-harmless); `--from-main` with `--runner`/`--child` raises the frozen `invalid-config` text.
  - Init branch: `config_uncommitted` pre-check before any prompt (exit 2, nothing written); `from_main` passed into `C.InitOptions`; `commit_paths` extended with created/updated `guidance` agent-rule targets, only inside a git checkout (`git_root is not None`).
  - Reroute: condition widened to `{"initialization-required", "config-uncommitted"}`; on `("missing", typed)` raises `config_uncommitted(scope_dir)`, falling back to `config_uncommitted(cwd)` (scope paths are usually absent from the worktree since uncommitted main files never arrive); otherwise the old "no ptest project … — run ptest init there" line, byte-identical.
  - `_warn_uncommitted_config` (frozen 1 / N>1 texts, `progress.emit(..., quiet=quiet)`, try/except-all), called right after `_warn_stale_guidance`.
  - `where`: `config-uncommitted` warnings entry for `--json` (exit 0); one stderr line after stdout for human.
  - `doctor` branch: first-statement raise on `config-uncommitted` (before `--fix`/`--probe`/offline/consent).
  - `_uncommitted_mention` (frozen text, `include_agent_rules=True`, None on empty/exception); printed after the offline grid next to `_fix_mention`, and after `_run_review_entry` returns in the online path when not declined and not `--json` (declined funnels through the static grid, so the line still appears exactly once; never for `--json`/`--fix`/`--probe`).
- `tests/ng/test_cli.py` (+440): 21 new `test_t2_*` tests (26 with params), frozen `_linked_worktree` helper copied verbatim, `_git_only_popen` helper, `commit_paths` added to the init JSON key-set assertion; 4 pre-existing doctor/Popen-ban tests given a git-only carve-out (see stragglers).
- `tests/ng/test_init_smoke.py` (1 line): `commit_paths` added to the init JSON key-set assertion.

No other source file touched. `graphify update .` run.

## Acceptance item → test

1. Bare/scoped run in wt exits 2, `config-uncommitted:` + `Do not run ptest init here`, runner never executes, no `run ptest init there` / `run ptest init to update` advice → `test_t2_bare_and_scoped_run_in_worktree_exit_config_uncommitted[() | (tests/test_x.py,)]` (operations.execute mocked to fail). Deviation: the design's literal `main(("tests",))` cannot reach the run path — bare `tests` is an `unknown command` in the closed prefix grammar (pre-existing); `tests/test_x.py` exercises the intended scoped-run path.
2. Reroute names `main/api/.ptest.toml`; non-worktree keeps old line → `test_t2_path_reroute_names_main_child_config`, `test_t2_missing_path_outside_worktree_keeps_old_line` (exact old-line match). Note: the wt setup commits `api/README.md` so `wt/api` exists — `config_uncommitted` needs an existing directory (`_absolute_directory` raises on missing paths, T1 behavior); the cwd fallback covers scopes absent from the worktree.
3. Init refuses (no prompt via failing `input`, nothing written); `--json` error doc; `--from-main --agents none` copies bytes, exit 0, stopgap warning, no `Commit these files` reminder; `--json` gives `commit_paths == []` + `config-uncommitted` warning; `--from-main` with `--runner`/`--child` → `invalid-config`; main tree snapshot (paths+bytes) identical → `test_t2_init_refuses_in_worktree_without_prompt`, `test_t2_init_json_error_document_in_worktree`, `test_t2_init_from_main_copies_with_stopgap_and_no_commit_reminder`, `test_t2_init_from_main_json_has_empty_commit_paths`, `test_t2_from_main_rejects_runner_and_child[runner|child]`.
4. register/plan/history `--json` error docs; `where --json` warnings entry + human stderr line; doctor `--offline`/`--fix`/`--json` exit 2, `input` mocked to fail → `test_t2_register_plan_history_json_error_in_worktree`, `test_t2_where_json_carries_warning_in_worktree`, `test_t2_where_human_shows_stderr_line_in_worktree`, `test_t2_doctor_refuses_before_consent_in_worktree`, `test_t2_doctor_json_refuses_in_worktree`.
5. Run warning: exact single line; `-q` suppresses; stdout+exit identical committed vs uncommitted; silent when committed / non-git; monorepo child listed; raising helper leaves run unaffected → `test_t2_run_warns_once_for_untracked_config`, `test_t2_run_warning_quiet_and_committed_and_nongit`, `test_t2_run_warning_matches_committed_outcome`, `test_t2_run_warning_lists_untracked_monorepo_child` (`web/tests/test_x.py` scope; bare monorepo runs execute nothing without changes), `test_t2_run_unaffected_when_uncommitted_check_raises`.
6. `init --json` in git repo lists `.ptest.toml` + created rule paths (all exist on disk); non-git gives `[]`; key-sets updated → `test_t2_init_json_commit_paths_lists_config_and_rules`, `test_t2_init_json_commit_paths_empty_outside_git`.
7. Doctor mention after offline grid when uncommitted; absent when committed and for `--json` → `test_t2_doctor_mention_after_offline_grid_for_uncommitted`.
8. Scoped run green → `ptest --workers 2 --queue-timeout 1800 tests/ng/test_cli.py tests/ng/test_init_smoke.py` → `ptest: passed · 382 tests · 56.5s` (commit 3343450, clean tree).

## Straggler files (existing tests updated)

`tests/ng/test_cli.py` (owned by T2) — 4 tests failed identically on the clean chain tip 5159175 (verified in a detached scratch worktree at that SHA, then removed): `test_static_dispatch_is_read_only_redacted_and_contract_valid[{True,False}-doctor]`, `test_doctor_from_monorepo_root_renders_declared_rows_and_worksheet`, `test_offline_doctor_stays_static_without_launch`. Cause: their blanket `subprocess.Popen` ban vs T3's specified one-shot `git ls-tree` for the `config.uncommitted` finding (`doctor.inspect_workspace` → `_config_findings`). `pytest.fail` inherits `BaseException`, so it escapes T1's `except Exception` best-effort guard — test-mock interaction only; production behavior is per design. Minimal update: Popen mock now allows `git` and still fails on any runner/network subprocess (new `_git_only_popen` / `_forbid_launch_except_git` helpers); assertions unchanged.

Neighbor sweep (unowned, untouched, all green): `ptest --workers 2 --queue-timeout 1800 tests/ng/test_run_output.py tests/ng/test_natural_loop.py tests/ng/test_init.py tests/ng/test_acceptance.py tests/ng/test_doctor.py` → `ptest: passed · 299 tests · 1m50s`. No stragglers there.

## Deviations from design §3.4 (documented, texts still byte-for-byte)

- Reroute missing-branch additionally falls back to `config_uncommitted(Path.cwd())` when the scope dir yields None. Reason: scope paths are usually absent from the worktree, and T1's `config_uncommitted` returns None for nonexistent directories — without the fallback, acceptance item 1 (frozen helper setup, main root config untracked) prints the old line. The raised problem is the identical frozen one.
- Doctor-branch mention prints only when not declined: the declined path funnels through `_doctor_static_output`, which already prints the line — unconditional printing would duplicate it. Visible in both outcomes, exactly once.
- Item-1 scope `("tests",)` replaced by `("tests/test_x.py",)` (bare word is `unknown command` in the closed grammar, pre-existing behavior T2 does not own).

## Honest close

- Implemented: all §3.4 bullets + 8 acceptance items. Verified: 382-test scoped file run green; 299-test neighbor sweep green; main-checkout snapshot unchanged around `--from-main` (test-pinned).
- Not verified: the global suite (`--full` is the orchestrator's gate).
- Deferred: none.
- Discovered but not fixed (out of scope, for integration): `executability`'s "run ptest init from the repository root" fix text is still reachable in partial-doctor scans (design §7 follow-up); the 4 straggler tests above were already red at the chain tip due to T3's `git ls-tree` in offline doctor.
- Confidence: high — every acceptance item maps to a named test that failed pre-change (or passes via T1 core with a T2 regression pin) and passes now; no source file outside `cli.py` touched.

## Fix round 1 (audit response, branch feature/worktree-safe-config-T2)

Worktree: `/home/ingmar/worktrees/ptest/cc-worktree-safe-config/ptest-T2` (existing; no new worktree).
Strict TDD: every new/strengthened assertion below was observed failing
against the pre-fix code (or proved non-vacuous by mutation / a sibling
fail-first run — noted per item), then fixed.
Verify: `ptest --workers 2 --queue-timeout 1800 tests/ng/test_cli.py
tests/ng/test_init_smoke.py tests/ng/test_worktree.py tests/ng/test_doctor.py
tests/ng/test_config.py tests/ng/test_init.py` → `ptest: passed · 855 tests`.

1. Stale-guidance warn before config-uncommitted refusal (BLOCKING):
   `main()` now skips `_warn_stale_guidance` when
   `resolution.problem.code == "config-uncommitted"` (`cli.py` ~3450).
   Only call site (verified by grep); other "run ptest init" hints
   checked: the reroute old-line fires only when `config_uncommitted`
   is None for scope dir and cwd, `_fix_mention` names `--fix` not
   init, `executability` fix text is unreachable here (doctor refuses
   first, D6). Test: item-1 test parametrize is now `[(), ("-k",
   "foo")]` with `agent_rules.guidance_outdated` monkeypatched to
   True — both params failed pre-fix, pass now.
2. Init-refusal TTY pin: test sets `sys.stdin.isatty -> True` with
   `input -> fail`, asserts exit 2 plus a before/after tree snapshot
   ("nothing written"). Pin is live: the same TTY+input tripwire was
   observed firing in this exact init path (item 8 pre-fix run).
3. Non-git `commit_paths` pin: test uses `--agents all` (rule file
   asserted on disk) with `commit_paths == []`. Mutation-proved:
   deleting the `git_root` guard makes it fail; guard restored.
4. Doctor `--json` negative: test creates a new untracked marked
   `.claude/skills/ptest/SKILL.md`, asserts the helper is non-empty
   while `--json` stdout lacks the line; also asserts
   `index("0 calls") < index("not committed:")` (mention after grid).
5. Carve-out narrowed: `_git_only_popen` now allows only argv
   containing `"ls-tree"` and is documented doctor-only; the 14
   static-dispatch cases, the monorepo doctor test, the offline-static
   test and the new hostile-paths test run under the full
   `_forbid_launch` ban; `_forbid_launch_except_git` deleted. Item-9
   re-check: the carve-out is still needed exactly once — the
   doctor-mention test in a real git repo, where genuine `git ls-tree`
   output is required (probe-recorded argv confirms only `ls-tree`
   fires from doctor). Fixture `git add/commit` scaffolding runs
   outside the gate via explicit `_gate`/`_ungate`.
6. Doctor mention sanitized: `_uncommitted_mention` wraps the line in
   `render.terminal_text` (`cli.py` ~2131). Test feeds
   `("evil\x85/.ptest.toml", "bad\u2028/.ptest.toml")` through the
   mocked helper and asserts both raw chars absent (failed pre-fix).
7. Run-warning outcome equality uses the real echo runner (stub
   removed; checkout moved inside the fixture domain for scheduling).
   Probe finding: the echo child writes past `capsys` to the fd, so
   the test uses `capfd` and asserts `"hello" in out` plus
   byte-identical stdout and exit 0 in both states.
8. `init --from-main` outside a linked worktree on a TTY refused
   before the agents prompt: new `elif` raises `invalid-config` with
   T1's exact message reused via `config_api._FROM_MAIN_REFUSAL` (no
   text duplication; cross-module private access is deliberate). The
   `.path is None` condition preserves the existing-config
   from_main-is-ignored path. Test (isatty True, input fails) failed
   pre-fix with the prompt firing; green now.
9. (T1 file, allowed) `worktree._uncommitted_inner` returns `()`
   without any subprocess when `git_root(os.path.realpath(root))` is
   None (deferred import — no cycle with `config`). `realpath` keeps
   the existing symlinked-root contract green. New
   `test_uncommitted_config_files_non_git_spawns_no_subprocess`
   (Popen `pytest.fail`, incl. `include_agent_rules`) failed pre-fix;
   full `test_worktree.py` (43 tests) green.
