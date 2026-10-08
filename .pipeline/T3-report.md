# T3 report — opt-in policy core, worktree/uninstall recognition, CLI wiring

status: DONE (one documented worktree-local red test that is a proven
missing-barrier artifact; green as soon as T1's guide bytes land — see
"Barrier finding" below)
taskWorktree: /home/ingmar/worktrees/ptest/cc-test-policy/ptest-T3
taskBranch: feature/test-policy-T3
taskCommit: 09f93fe
baseCommit: bbf1260
filesOwned (touched — all inside the owned set, nothing else):
- src/ptest/agent_rules.py
- src/ptest/resources/test-policy.md (new)
- src/ptest/worktree.py
- src/ptest/uninstall.py
- src/ptest/cli.py
- src/ptest/init_render.py
- src/ptest/help.py
- pyproject.toml
- tests/ng/test_agent_rules.py (1-line cap bump only)
- tests/ng/test_help.py (markers only)
- tests/ng/test_init_render.py (+2 tests)
- tests/ng/test_worktree.py (+2 tests)
- tests/ng/test_policy_optin.py (new, 80 tests)
NOT touched (needed no changes): tests/ng/test_uninstall.py,
tests/ng/test_cli.py, tests/ng/test_init.py, tests/ng/test_doctor.py,
tests/ng/test_init_commit_reminder.py — all pass unmodified.
No database migration generated. No push, no merge, no deploy.
`.pipeline/` and local-only tooling never staged or committed.

## What was built

Policy core (`src/ptest/agent_rules.py`):
- `TEST_POLICY_PATH = "docs/ptest-test-policy.md"` (:19).
- `_test_policy()` returns `resources/test-policy.md` bytes (:384);
  `_PREVIOUS_TEST_POLICY_SHA256S = frozenset()` (:397).
- `_block(name, *, test_policy=False)` (:406): base variant byte-identical
  to today; policy variant appends exactly one reference line after the
  guide line (AGENTS.md prose, CLAUDE.md/GEMINI.md `@docs/...`).
- `block_variant(text, name)` (:415) returns None | "base" | "policy" and
  raises `invalid-config` on extra text, changed policy line, two blocks,
  or unbalanced markers. `_managed_state` (:497) keeps its signature and
  delegates (`block_variant(...) is not None`).
- `test_policy_installed(root)` (:438): True when any instruction block is
  the policy variant; False on any problem (missing, symlink, non-regular,
  oversized, unreadable, non-UTF-8, malformed).
- `preview`/`apply(..., test_policy=False)` (:1184/:1429): existing call
  shapes unchanged; effective policy = flag OR recorded variant. Plan
  actions use the frozen strings (`create/update/already present
  docs/ptest-test-policy.md`, `add test-policy reference to <name>`);
  ActionRecord details stay in the frozen vocabulary. Base-only runs are
  byte-identical to 0.5.2. Policy file is hash-tracked like the guide
  (`_policy_kind`, :1038); user-edited copies raise `already-exists`
  before any write; `refresh` rewrites it in place only for previous
  hashes and only when the policy is recorded, never adds it otherwise,
  and never touches instruction files. Rollback reuses the existing
  created/updated machinery (expect_bytes + identity).
- Barrier B2/B4 equivalents implemented verbatim from design section 12
  in the owned files: `import re`, `TEST_POLICY_PATH`, the bbf1260 hash
  with its comment in `_PREVIOUS_GUIDE_SHA256S`, and the full
  coverage-scanner block (`InstructionLine`, `InstructionScan`,
  `coverage_instruction_lines`, :421-540). See "Barrier finding".
- `src/ptest/resources/test-policy.md`: byte-exact design 4.2 text
  (verified programmatically), no digits, trailing newline;
  `pyproject.toml` package-data gains `resources/test-policy.md`.
- `src/ptest/worktree.py`: `AGENT_RULE_FILES` gains
  `docs/ptest-test-policy.md` right after `docs/ptest-agent.md`.
- `src/ptest/uninstall.py`: `_decide_policy` (current-or-previous hash
  removes, edited keeps), `_decide_block` accepts both variants via
  `block_variant` (hand-edited blocks stay skipped, bytes untouched),
  `docs/` pruned when the policy unlink empties it.

CLI (`src/ptest/cli.py`):
- Grammar: `init [--test-policy | --no-test-policy]` (combined/repeated
  rejected: "init test-policy modes cannot be combined or repeated");
  `rules [--apply] [--test-policy]` (anything else: "rules accepts only
  --apply and --test-policy"); `guide [TOPIC] | guide --write PATH`
  (unknown/path-like topic: exit 2 "unknown guide topic; topics: ...",
  input never echoed; `guide TOPIC --write x` rejected with nothing
  written). Topic table reads `checklist.recipe_names()` with a fallback
  to `_RECIPE_FILES` order until barrier B5 lands (`_guide_topics`,
  :291).
- Prompt (`_init_test_policy`, :3121): only in `ptest init` when stdin is
  a TTY, CI unset, not --json, not --dry-run, no policy flag, agents
  answer not none, policy not recorded. Default No; only y/yes accepts;
  exact design text on stderr with the planned change lines.
- Change lists: init stderr `ptest: test policy: will change: ...`
  (`would change` under --dry-run, `ptest: test policy: already
  installed` when nothing pending); `rules --apply --test-policy`
  prints `will change: ...` then `applied: ...` on stdout
  (`test policy: already installed`, no write, when nothing pending).
- Conflicts (`init_render.conflict_lines`, sanitized via
  `terminal_text`): init human output block, init --json stderr only,
  rules stdout after the preview/applied line — whenever the policy is
  requested or recorded. Never edited.
- Doctor wiring: `_test_policy_outputs` + the three call sites exactly
  per design B6 (:2264, :2308, :2396, :2355). Without T2's modules it
  returns `(None, "")`, so doctor output is unchanged in this worktree.
- `src/ptest/help.py`: init/rules/guide/uninstall/doctor topics and the
  overview lines document the flags, the topic, and the Test policy
  section. No banned terms.

## Verification (all `ptest` from the worktree root, never pytest/vitest)

- `ptest tests/ng/test_policy_optin.py` → 79 passed, 1 skipped
  (the skip is the T1-gated `guide tests` test).
- `ptest tests/ng/test_agent_rules.py tests/ng/test_worktree.py
  tests/ng/test_init_render.py tests/ng/test_help.py` → 192 passed,
  1 failed — the failure is `test_previous_hashes_cover_main_pre_change_guide:975`,
  proven below to be the missing-barrier artifact, not this diff.
- `ptest tests/ng/test_cli.py tests/ng/test_init.py` → 444 passed.
- `ptest tests/ng/test_policy_optin.py tests/ng/test_uninstall.py
  tests/ng/test_doctor.py tests/ng/test_init_commit_reminder.py` →
  316 passed, 1 skipped.
- Adjacent non-owned suites (change-surface safety):
  `test_install|test_init_changed|test_resources|test_agent_eval|test_checklist`
  → 78 passed; `test_acceptance|test_doctor_init_integration` → 24 passed.
- TDD: the new suite failed 59/76 on the empty shape (missing
  attributes/shapes confirmed before implementing), then went red→green;
  four of my own test-expectation bugs were corrected to the frozen
  semantics (upgrade action only for base→policy in place, etc.). No
  test or assertion was weakened — except the single mandated cap bump
  100→109 at test_agent_rules.py:180, which the design fixes.
- `graphify update .` run in the worktree.

## Barrier finding (please read before merging)

The transcription step in design section 0 never ran: the chain base is
plain bbf1260 — `coverage_instruction_lines` and `_test_policy_outputs`
are absent everywhere, the guide is still 100 lines, and
`checklist.recipe_names` does not exist. No barrier commit exists on any
branch (checked `git log --all`). Per the task text ("build what you own
against the frozen interfaces, declaring the missing shape locally ...
return BLOCKED only if genuinely impossible") I did not stop: the B2/B4
hunks live in `agent_rules.py` and B6 in `cli.py`, whose owner of record
is T3, so I implemented those hunks verbatim from design section 12 in
my branch (commit 09f93fe). Seams recorded for the orchestrator:

1. B1 (guide `## Writing tests` bytes) and B5 (`checklist.recipe_names`)
   are in files I must not touch (T1 owns them). `cli._guide_topics`
   falls back to `_RECIPE_FILES` order until B5 lands; delete the
   fallback when `recipe_names` exists.
2. Exactly one worktree-local red test follows from the missing B1:
   `test_previous_hashes_cover_main_pre_change_guide:975` asserts the
   current guide hash is NOT in `_PREVIOUS_GUIDE_SHA256S`, while T3
   acceptance mandates adding 5ac1f26 (the bbf1260/current guide hash)
   to that set. Both hold jointly only with B1's new guide bytes, which
   I cannot add. Proof (no tree files touched): applying B1's
   replacement in memory yields a 109-line guide (+9, matching the cap
   bump) whose sha256 differs from 5ac1f26, so the assertion passes as
   soon as T1's guide lands. The test itself is untouched (not mine to
   weaken), as are the guide files. All six other assertions in that
   test pass with my change.
3. If the barrier is still applied to the chain before the merge, its
   B2/B4/B6 hunks are byte-identical to mine (transcribed from the same
   section 12), so re-applying them must be skipped or it will
   conflict with this branch.

## Acceptance anchoring (T3 checkboxes)

- Fresh `apply(test_policy=True)` creates guide + policy bytes +
  policy-variant block; base blocks upgrade in place, other bytes
  untouched; second run reports already-present and writes nothing:
  test_policy_optin.py `test_apply_test_policy_creates_...`,
  `test_preview_lists_exactly_...`.
- `apply()` without the flag never creates the policy file or variant
  (`test_apply_without_flag_never_installs_policy`); with a recorded
  policy it converges at both API and CLI level
  (`test_apply_without_flag_converges_when_policy_recorded`,
  `test_rules_apply_without_flag_converges_recorded_policy`).
- current→already-present, previous→updated by apply and refresh,
  edited→`already-exists` before any write, refresh skips it,
  `guidance_outdated` true only for previous hashes (monkeypatched set):
  covered in test_policy_optin.py.
- `block_variant`/`_managed_state` accept exactly the two variants;
  extra text, changed policy line, two blocks, unbalanced markers raise
  `invalid-config`, apply writes nothing: covered.
- Mid-apply failure rolls back (policy file removed, AGENTS.md restored
  byte-for-byte): `test_mid_apply_failure_rolls_back_policy_writes`.
- Uninstall removes un-edited policy (current or previous hash) and
  either-variant blocks (pre-ptest bytes restored, block-only files
  unlinked, `docs/` pruned when empty); keeps edited policy (`kept,
  edited`); never rewrites hand-edited blocks: covered (CLI-level via
  `main` + fixture domain).
- `AGENT_RULE_FILES` includes the policy file; untracked policy file
  reported; both variants count as managed: test_worktree.py.
- CLI grammar exactly D8 incl. conflict/repeat rejection with exit 2
  before any write: covered. Prompt matrix (TTY-y, empty/EOF/anything-
  else declines; pipe, CI, --json, --dry-run, --no-test-policy, agents
  none, recorded → no prompt, no install): covered.
- Change lists before any write (init stderr will/would-change, rules
  stdout will-change→applied, already-installed): covered. Dry-run and
  `rules --test-policy` write nothing (tree bytes compared): covered.
- Conflict header + `file:line: text` lines, sanitized, never edited:
  covered (init human, init --json stderr-only, rules stdout,
  recorded-policy flows).
- `init --json --test-policy` validates with no new keys (exact key-set
  assertion) and `commit_paths` gains the policy file: covered.
- `guide TOPIC` prints exactly the recipe per topic; unknown, path-like
  (`../x`, `/etc/passwd`, `tests/../x`) and `TOPIC --write` exit 2 with
  nothing written; plain `guide`/`--write` unchanged: covered. `guide
  tests` gated with `skipif` on `"tests" in topics` (skips pre-T1).
- help.py documents flags/topic; test_help markers updated: covered.
- Barrier hunks in agent_rules.py/cli.py match section 12 verbatim
  (B4 block, B6 function + call sites, B2 hash/import/const).
- Abuse twins: symlinked policy target (unsafe-path), oversized
  instruction file (invalid-bound), user-edited policy (already-exists
  / refresh-skip / uninstall-kept), hand-edited block (invalid-config
  / uninstall-skipped, bytes unchanged): all covered.
- No migration generated. Eval/SCENARIO_IDS/scorer, recipes/tests.md,
  README, changelog, agent-guide.md reword: T1's, untouched.

## Review passes (dan-jefferies-agent)

Pass 1 — re-read the diff (`git diff --cached`, full file re-reads of
agent_rules/cli/uninstall/init_render/help hunks). Findings fixed
before commit: (a) new test module docstring promised an
oversized-instruction twin that did not exist — added
`test_oversized_instruction_file_is_refused_before_any_write`;
(b) guide `--json`/`--write`-first invalid combos must keep the old
"unknown inspection option" message (existing test_cli contract) while
bare words get the topic message — parser now splits on the leading
dash; (c) two of my own tests wrongly expected the upgrade action for
block-less files and refusal for text appended after the block —
corrected to the frozen semantics (append action; outside-block text is
tolerated, inside-block edits refuse).
Pass 2 — re-anchored above per checkbox with file:line citations.
Pass 3 — smell sweep. Bugs: TOCTOU windows in policy planning use the
same expect_bytes/identity machinery as the guide path, plus a
re-check of previous-hash membership at update time (stricter than the
guide, justified: the policy file is new attack surface for clobbering
user edits). No fallbacks/stubs/TODOs. Tests are not vacuous: e.g.
`test_apply_test_policy_creates_policy_file_and_variant_block` fails
against a no-op apply (no file, base block, `changed is False`).
Contract drift: every `preview`/`apply`/`_block`/`render_init` caller
checked (`scripts/agent_eval.py:92` old shape still works; uninstall is
the only `_block` caller and passes the new kwarg). Duplicates: no
existing scanner/reference-line/conflict helpers existed (grepped
`coverage_instruction_lines`, `conflict_lines`, `test_policy` —
zero hits before this diff). Security: hostile instruction text is
sanitized in both renderers (ANSI/bidi/control test); guide topics
never echo input and never leave the recipe table (traversal tests);
policy prompt is TTY-only, default No. Not applicable with reasons:
money/math (no numbers in policy text by design), legacy deletion
(`_managed_state` stays as a frozen-signature wrapper), deps (none
added), compat shims (none — flag defaults preserve every old call
shape).

## Honest closing

- Claimed-vs-shipped delta: none — every T3 checkbox above is
  implemented and tested, except the single worktree-local red test
  documented under "Barrier finding" (needs T1's B1 bytes, forbidden
  files for this task).
- Stubbed/TODO/deferred: the `checklist.recipe_names` fallback in
  `cli._guide_topics` (delete after T1/B5 merges); T2's
  `policy_facts`/`policy_render` are consumed best-effort via the
  barrier call sites (no-op until T2 merges).
- Out-of-scope but smells off (not fixed): nothing new found; the
  stale `.pipeline/T*-report.md` files from prior features were left
  in place except this T3 report, which overwrites the stale one per
  the task instruction.
- Owned-file set vs touched-file set: identical ceiling — 13 files
  touched, all in the owned list; 5 owned test files needed no edits
  and pass unmodified.
- Implemented: everything above. Verified: all listed ptest runs with
  observed outputs. Not verified: `ptest --full` (orchestrator's gate,
  explicitly out of scope for tasks) and post-merge behavior with T1's
  guide/T2's modules (covered by design, proven for the guide hash by
  the in-memory B1 construction). Deferred: nothing. Discovered but
  not fixed: the missing barrier transcription (orchestrator-level,
  flagged above).
- Confidence: high — the full owned scope plus adjacent suites are
  green, the one red test has a proven external cause and a
  merge-time resolution, and every acceptance clause maps to a passing
  test cited above.

---

# T3 fix report (attempt 2, dan-jefferies-agent) — 2026-10-08

Worktree: /home/ingmar/worktrees/ptest/cc-test-policy/ptest-T3 (committed
below; this appendix itself is NOT committed — `.pipeline/` is never staged).
Base: prior T3 branch head plus the gate-repair commits (a50b393 at start).

## Fix 1 (HIGH) — recorded-policy init without agents now takes the refresh path

`src/ptest/cli.py` init dispatch: added `wants_apply = bool(agents) or
policy_consent`. `preview`/`apply` run only when agents were chosen or
`--test-policy` was given; otherwise init falls into the existing refresh
branch (its `not effective` guard became `not wants_apply`, so a recorded
policy no longer excludes it). Display (`will change`/`already installed`)
and conflict listing still key off `effective`, and are vacuous when there
is no plan. Recorded-policy + user-edited policy file now exits 0 with the
file untouched; recorded-policy + block-less GEMINI.md leaves it
byte-identical with no commit-path addition.

## Fix 2 (HIGH) — change lists now include appended instruction files

`_test_policy_change_lines` (`cli.py:303`) appends every `append managed
reference to <name>` action after the create lines. All three call sites
(rules `--apply --test-policy`, init will-change, consent prompt) pass
plans previewed with the policy in effect, so each listed append is a file
the apply will write with the policy-variant block. Per-file actions in
`preview` are mutually exclusive, so no file is ever listed twice.

## Fix 3 (MEDIUM) — non-regular policy target abuse tests

`tests/ng/test_policy_optin.py`: parametrized FIFO/directory test asserting
`apply` and `preview` with `test_policy=True` raise `unsafe-path` with no
writes (no AGENTS.md, no guide, target identity preserved), and a
parametrized uninstall test asserting exit 0, `skipped` in output, and the
FIFO/directory untouched. No source change — the `_read_regular` /
`_classify` ladders already handled it; the tests pin the contract.

## New tests (8, all in `tests/ng/test_policy_optin.py`)

- `test_non_regular_policy_target_is_refused_before_any_write[fifo|directory]`
- `test_uninstall_skips_non_regular_policy_target[fifo|directory]`
- `test_rules_apply_lists_appended_instruction_file_before_writing`
  (block-less CLAUDE.md: will-change lists create + upgrade + append,
  applied echoes the append, file gains the policy variant)
- `test_noninteractive_init_with_recorded_policy_keeps_edited_policy`
  (exit 0, edited policy bytes unchanged, no will-change)
- `test_noninteractive_init_with_recorded_policy_leaves_blocks_alone`
  (exit 0, GEMINI.md byte-identical, no will-change)

## Verification (all `ptest` from the ptest-T3 worktree root)

- `ptest tests/ng/test_policy_optin.py` → 87 passed, 1 skipped (the
  pre-existing T1-gated `guide tests` skip).
- Non-vacuity: with `HEAD`'s `cli.py` restored, the 3 Fix-1/Fix-2 tests
  fail (3 failed); fixed file restored afterwards (diff re-verified).
- `ptest test_policy_optin test_uninstall test_doctor
  test_init_commit_reminder` → 325 passed, 1 skipped.
- `ptest test_cli test_init` → 444 passed.
- `ptest test_agent_rules test_worktree test_init_render test_help` →
  193 passed (the barrier red test from attempt 1 is green in this
  checkout — T1's guide bytes have since landed here).
- No test or assertion weakened; no push, no merge, no deploy.

## Review passes (dan-jefferies-agent)

Pass 1 — re-read `git diff` end to end. One non-obvious case checked:
`--agents none` (explicit) + recorded policy still skips refresh via the
`not agents_explicit` guard, exactly as before; `--agents all`
+ recorded still converges via apply. No behavior change except the
targeted recorded-policy/no-agents/no-flag path.
Pass 2 — each finding's criterion maps to a passing test cited above.
Pass 3 — no new helpers or signatures (one new local); `_detail`/
`ActionRecord` vocabulary untouched; existing will-change assertions are
all substring-based and still pass; prior-report suites re-run green.
Security: the consent prompt now shows every file the apply writes
(Fix 2 closes a consent-gap); refresh remains the only writer on the
non-interactive path and never touches instruction files.

Honest closing — implemented: all three findings. Verified: suites above
with observed outputs. Not verified: `ptest --full` (orchestrator's gate).
Deferred: nothing. Confidence: high — narrow diff, regression tests proven
red-before/green-after, adjacent suites green.
