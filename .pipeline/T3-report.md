# T3 report — init commit reminder lists every uncommitted ptest file

- taskWorktree: /home/ingmar/worktrees/ptest/cc-doctor-offline/ptest-T3
- taskBranch: feature/doctor-offline-T3
- base: 09f6f40 (design freeze) / 985d43e (0.3.7)
- status: DONE except one integration-owned assertion update (see §5)
- commits: `2274a14` T3: init commit reminder lists every uncommitted ptest file;
  `e39874c` T3: keep from-main stopgap copies out of commit_paths

## 1. What changed (owned files only)

- `src/ptest/config.py`
  - NEW `_merged_commit_paths(boundary, config_root, written)` (:1398): `written`
    first (deduped, order kept), then `worktree.uncommitted_config_files(boundary,
    include_agent_rules=True)`, then — only when `config_root != boundary` and
    inside it — `uncommitted_config_files(config_root, include_agent_rules=False)`
    prefixed with the boundary-relative dir. Deduped, stable order, `()` when
    boundary is None, never raises (falls back to written).
  - NEW `_existing_with_commit_paths(physical_cwd, result)` (:1539): boundary via
    `_git_boundary` with raised `Problem` treated as None; merges with `written=()`
    and `config_root=result.target.parent`. Applied at all four `init_project`
    `_existing_result` return sites (:1559, :1579, :1584, :1670).
  - Monorepo + standalone CREATED returns merge written names through the helper
    (:1635, :1678). PREVIEW/`--dry-run` and `_init_from_main` returns untouched.
  - `--from-main` guard (:1558-1564): an existing result under `options.from_main`
    keeps `()` — a from-main config is a stopgap copy that must not be committed
    in that worktree; the stopgap warning governs it.
- `src/ptest/init_render.py` — `_commit_reminder_lines` docstring (:393) now reads
  "every uncommitted ptest file". No behavior change (still renders from
  `result.commit_paths`, still sanitizes via `terminal_text`).
- NEW `tests/ng/test_init_commit_reminder.py` — 13 tests (see §3). No migration.

`cli.py` untouched: the existing guidance merge (~2420) appends
`agent_rules.apply` targets verbatim and keeps working; JSON `commit_paths`
flows through `serialize_init_result` unchanged.

## 2. Verification (all through ptest, capped `--workers 2 --queue-timeout 1800`)

- `ptest --workers 2 --queue-timeout 1800 tests/ng/test_init_commit_reminder.py`
  → **12 passed** (then 13/13 after adding the from-main test; see run3).
- `ptest --workers 2 --queue-timeout 1800 tests/ng/test_init_commit_reminder.py tests/ng/test_init.py`
  → **1 failed, 82 passed** (`/tmp/t3_run3.log`); the single failure is
  `test_init.py:1185`, owned by the integrator (§5). All 13 T3 tests pass.
- `ptest --workers 2 --queue-timeout 1800 tests/ng/test_init_commit_reminder.py tests/ng/test_init.py tests/ng/test_agent_rules.py tests/ng/test_agent_doctor_acceptance.py`
  → **2 failed, 137 passed** (`/tmp/t3_scoped.log`, pre-guard run); after the
  guard only the §5 assertion remains. `test_agent_rules.py` and
  `test_agent_doctor_acceptance.py` contain zero `from_main` references, so the
  guard cannot affect them — their green stands.
- No-op sensitivity: the core tests fail on pre-fix code by source inspection
  (`_existing_result` built `InitResult` without `commit_paths`, i.e. `()`), so
  `test_unchanged_config_*` cannot pass vacuously. (Pre-fix runner observation
  was not cleanly captured: the first queued run raced mid-run edits; honest
  state — failure proved by code path, not by a clean pre-fix run.)
- No wall-clock assertions; every `invoke` is in-process `cli.main`.

## 3. Acceptance criteria (§T3.1), each ticked

- Untracked `.ptest.toml` + `api/.ptest.toml` + `web/.ptest.toml` (v2) +
  `.claude/skills/ptest/SKILL.md` → text `ptest init` prints `Commit these files:`
  with all four; `--json` `data.commit_paths` equals the same ordered list
  (`test_unchanged_config_*`, 3 tests).
- After `git add -A` + commit: no reminder, `commit_paths == []`
  (`test_committed_everything_clears_reminder`).
- Fresh standalone create with pre-existing untracked `docs/ptest-agent.md` →
  `(".ptest.toml", "docs/ptest-agent.md")`, written first, deduped
  (`test_fresh_standalone_create_orders_written_first_deduped`).
- `--agents claude` with a pre-existing legacy skill file: skill appears exactly
  once, guide created this run appended, no duplicates
  (`test_agent_rule_files_created_this_run_appended_once`).
- Untracked `notes.toml`/`TODO.md` never listed
  (`test_untracked_non_ptest_files_never_listed`).
- Non-git dir → `[]` (`test_non_git_directory_gives_empty`).
- `--dry-run` → no reminder and `[]`; PREVIEW `commit_paths == ()`
  (`test_dry_run_gives_no_reminder`).
- Nested config root → `("sub/.ptest.toml",)` boundary-relative
  (`test_nested_config_root_lists_boundary_relative_path`).
- `uncommitted_config_files` monkeypatched to `()` → falls back to written
  (`test_scan_failure_falls_back_to_written_list`).
- Control-character path rendered sanitized (`test_control_characters_render_sanitized`).
- From-main first + second run keep `()` (`test_from_main_second_run_keeps_empty_commit_paths`).

## 4. Smell sweep (core, with locations)

- Bugs: `_merged_commit_paths` prefix uses `relative_to` in try/except ValueError
  (`config.py:1413`); boundary-None returns `()` before any scan (:1408); helper
  never raises (:1425). `replace()` on frozen `InitResult` keeps all other fields.
- Duplicates: grepped `merged_commit|uncommitted` in `config.py` — no prior merge
  helper; reuses `worktree.uncommitted_config_files` and `_commit_paths` as specified.
- Wrong layer: merge lives in `config.py` next to `_commit_paths`; `cli.py`,
  `worktree.py`, `contracts.py` untouched (verified via diff).
- Workarounds/stubs/TODOs: none added.
- Contract drift: public signatures unchanged (`init_project`, `InitResult`,
  `rank_*` untouched); new names are private (`_merged_commit_paths`,
  `_existing_with_commit_paths`); only caller of the cli merge is unchanged.
- Security: only ptest-owned names listed (worktree allowlist); git subprocess
  stays inside the existing 2 s bounded helper, fails closed to `()`; reminder
  paths go through `terminal_text`.

## 5. Claimed-vs-shipped delta (integration note — action required at merge)

ONE existing assertion needs the T3.2-permitted update, which T3 is mechanically
forbidden from making (T3 may not touch `tests/ng/test_init.py`):

- `tests/ng/test_init.py:1185`
  (`test_init_commit_paths_for_created_preview_existing`):
  `assert existing.commit_paths == ()` → `assert existing.commit_paths == (".ptest.toml",)`.
  Reason: the created `.ptest.toml` is genuinely uncommitted in that fixture, so
  the new rule correctly lists it. Tightens, never loosens.

No other existing assertion changed; `test_init_from_main_second_run_is_existing`
(`:1127`, `commit_paths == ()`) stays green via the §1 guard. No stubs, no
TODOs, no deferred behavior. Out-of-scope smell: none found.

Final status: implemented — §1 + 13 new tests; verified — §2 runs above;
not verified — clean pre-fix runner failure (code-path proof only);
deferred — §5 assertion update (integrator-owned);
discovered-but-not-fixed — none. Confidence: high — every criterion has a
passing test and the only red is the named, permitted, integration-side update.
