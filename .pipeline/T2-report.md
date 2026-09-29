# T2 report — Docs for update check and `ptest update`

Status: done (docs complete, committed c868a96; ptest-form gate green, 201 passed).

- taskWorktree: /home/ingmar/worktrees/ptest/cc-update-check/ptest-T2
- taskBranch: feature/update-check-T2
- commit: c868a96 ("Docs for update check and ptest update (T2)")
- base: d5bae4e (chain worktree HEAD at start)

## What was done (design section 5, all items)

1. **Agent guide** (`src/ptest/resources/repository-agent-guide.md`): appended
   the update-available row as the last row of `## Reading ptest output`,
   byte-exact per design 5.1 (U+2014 dash verified via repr). Joined the
   two-line "Untracked config" paragraph onto one line. Guide is exactly
   100 lines. `docs/ptest-agent.md` re-synced with `cp` and verified
   byte-identical (`cmp` clean).
2. **`src/ptest/agent_rules.py`**: appended the hash entry as the last
   element of `_PREVIOUS_GUIDE_SHA256S` (design 5.2 verbatim). The hash
   `2639d68b…` was corroborated as the sha256 of the pre-change guide
   (computed `sha256sum` on the untouched file before editing — matched).
3. **`tests/ng/test_agent_rules.py`**: added
   `test_previous_hashes_cover_pre_update_row_guide` (design 5.3 verbatim
   assertions). TDD: observed it FAIL before the implementation
   (AssertionError on the missing hash), then PASS after.
4. **README.md** (design 5.4 verbatim): section renamed to
   `### Update, pin a version, remove`; code block replaced with the
   `ptest update` / `--check` / `--version` / one-liner form; startup-check
   paragraph added with the exact S1/S2/S4 strings; troubleshooting row
   added. `ptest uninstall` and `PTEST_STATE_DIR` kept.
5. **`docs/installation.md`** (design 5.5): new `## Updating` section after
   `## One-line install` — grammar, verification (HTTPS-only, SHA-256,
   64 MiB cap, confined extraction, bundled install.sh), side-by-side
   bundles + atomic switch + failed-update-keeps-old, startup check
   (24 h cache, PTEST_STATE_DIR, 2 s bound, silent offline), opt-outs,
   S4 source refusal, exit codes 0/2/75.
6. **`docs/changelog.md`** (design 5.6): new `## Unreleased` section above
   `## 0.3.7` covering `ptest update`, the startup check, the agent-guide
   row, and the installer smoke check running with `PTEST_NO_UPDATE_CHECK=1`.
7. `graphify update .` run in the worktree (graph up to date; no stray
   files — `git status` shows only the 7 owned files).

Quoted user-visible strings are exactly the frozen S1/S2/S4/S5/S6/S7 texts
(U+2014 verified; the single U+2013 in README line 53 is pre-existing).
No other ptest output text was invented. No code or test files outside the
owned set were changed. No migration generated (none exists in this repo).

## Verification (dan-jefferies passes 1–3)

- Pass 1 (re-read own bytes): full `git diff` re-read. One finding while
  writing: changelog S1 quote drops the trailing space inside backticks —
  matches design 5.4/5.6's own rendering (invisible in Markdown); kept.
  No other findings; diff is 109 insertions / 9 deletions across 7 files.
- Pass 2 (acceptance criteria, design 5.7):
  - [x] Strings match 3.1 byte-for-byte (repr-checked).
  - [x] `docs/ptest-agent.md` byte-identical; guide 100 lines (`cmp`, `wc`).
  - [x] Scoped gate, passing: `ptest --workers 2 --queue-timeout 1800
    tests/ng/test_agent_rules.py tests/ng/test_resources.py
    tests/ng/test_init_changed.py tests/ng/test_help.py
    tests/ng/test_uninstall.py` → `ptest: passed · 201 tests · 10m17s`
    (first attempt expired after ~30 min in the saturated machine queue
    without executing; retry executed green). Same scope also verified
    via capped `.venv/bin/python -m pytest -n 2` → 134 + 67 passed.
    Covers the new test,
    `test_every_shipped_guide_version_hashes_into_previous_set`,
    the banned-terms test, and the README uninstall/PTEST_STATE_DIR
    assertions. Note: the ptest run printed `unknown-input: the scoped
    run ran on a dirty source tree` (transient run artifacts); verdict
    is `passed`, and the worktree is clean at c868a96.
  - [x] Only frozen strings quoted. [x] graphify run. [x] Committed in worktree.
- Pass 3 (smell sweep): docs-only change — no runtime paths, no new helpers,
  no contract drift (no code touched), no deps, no shims. Banned-term scans
  pass (`fingerprint`/`ready with caveats`/`expected:` absent from touched
  prose; guide words `baseline`/`coverage`/`graphify`/`fast-forward`/
  `fingerprint`/`expected:` absent). Pre-existing en dash (README:53)
  proven pre-existing via stash check (count 1 before and after).

## Honest closing

- Claimed-vs-shipped delta: none — every design 5.1–5.6 item shipped.
- Stubbed / TODO'd / deferred: none in the docs. The ptest-form gate
  re-run is pending on the machine-wide queue (not a doc gap).
- Out-of-scope smells (reported, not fixed): none found.
- Owned-file set vs touched-file set: identical — README.md,
  docs/installation.md, docs/changelog.md,
  src/ptest/resources/repository-agent-guide.md, docs/ptest-agent.md,
  src/ptest/agent_rules.py (hash entry only),
  tests/ng/test_agent_rules.py (one test only). Note: the task text's
  5-file list is superseded by design section 1/6 (binding corrections);
  `agent-guide.md` (repair text) was intentionally left untouched.
- implemented: all 7 file changes, committed as c868a96.
- verified: row byte-exactness, guide 100 lines + identical copies,
  hash corroboration, new-test red→green, 201 scoped tests green (capped pytest).
- not verified: none (ptest-form gate now green; see above).
- deferred: none. discovered-but-not-fixed: none.
- Confidence: high — the docs are byte-checked against the frozen
  interfaces and the T2 scope passes in both launcher forms
  (ptest: 201 passed; capped pytest: 201 passed).

## Integration notes (for the orchestrator / T1)

- Seam: T2 quotes T1's frozen strings (S1/S2/S4/S5/S6/S7, S4 refusal,
  exit codes 0/2/75). If T1's shipped strings differ by even one byte,
  README/installation/changelog/guide drift — run a grep cross-check
  after both merge.
- `test_every_shipped_guide_version_hashes_into_previous_set` passes with
  the new hash entry included (verified in the interim run, post-commit
  content identical to tested tree).
- secure-by-spec: docs-only task — no implementation surface; the
  applicable control (quote only frozen strings, invent no output text)
  was applied. Full loop not run (standing rule: skip for docs).
- No push, no merge, no deploy, no live-database contact. Worktree
  `.venv` built via `uv sync --locked --extra test`; no `.env` exists in
  the chain worktree so nothing was linked.

## Fix report — HIGH finding: update-available row missing from shipped guide (2026-09-29, attempt 2)

- Fix commit: 23233cf ("T2: restore guide row, hash entry, regression test
  (revert c59a347)"), worktree
  /home/ingmar/worktrees/ptest/cc-update-check/ptest-T2, parent c59a347.
- Decision: option (a) from the finding. Design 1.1/1.2 and section 6 are
  explicit that T2 owns `repository-agent-guide.md` (+ `docs/ptest-agent.md`
  copy), the one `agent_rules.py` hash entry, and one `test_agent_rules.py`
  regression test — so the c868a96 changes were in-lane and the c59a347
  revert is what broke the product. `git revert --no-commit c59a347`
  restores them byte-exact (diff of the fix is the exact inverse of c59a347).
- `agent-guide.md` untouched (no table row added): `git diff d5bae4e --
  src/ptest/resources/agent-guide.md` empty, zero `|` lines — it is the
  `ptest guide` repair text per design 1.1.
- Corroboration (each value by two routes):
  - Old-guide hash: `git show d5bae4e:src/ptest/resources/repository-agent-guide.md
    | sha256sum` → `2639d68b…` (matches the restored `_PREVIOUS_GUIDE_SHA256S`
    entry and the T2-report §2 hash claim).
  - Copies identical: `cmp docs/ptest-agent.md
    src/ptest/resources/repository-agent-guide.md` clean; both sha256
    `0c6f82ad…`; guide 100 lines (`wc -l`).
  - Managed again: `_guide_kind(read_text('docs/ptest-agent.md'), _guide())`
    → `"current"` (was `already-exists` Problem at c59a347); old hash in set
    → True; `ptest: update available:` in `_guide()` bytes → True.
  - Changelog claim ("agent guide has an update-available row") is true of the
    shipped resource again.
- Verification:
  - `ptest --workers 2 --queue-timeout 1800 tests/ng/test_cli.py
    tests/ng/test_help.py tests/ng/test_contracts.py` → `ptest: passed ·
    489 tests · 7m42s` (exit 0). Note: the specified 4-path form including
    `tests/ng/test_update.py` collects **0 items** (`2 workers [0 items]`,
    exit 5) because `test_update.py` is T1's new file and does not exist in
    the T2 worktree — T1/T2 run in separate worktrees per design 6, so that
    file cannot be exercised here; the three in-worktree paths all pass. The
    run printed `unknown-input: the scoped run ran on a dirty source tree`
    (transient run artifacts); verdict is `passed` and `git status` is clean
    at 23233cf.
  - `.venv/bin/python -m pytest -n 2 tests/ng/test_agent_rules.py
    tests/ng/test_resources.py tests/ng/test_init_changed.py` → 71 passed,
    covering the restored `test_previous_hashes_cover_pre_update_row_guide`,
    `test_every_shipped_guide_version_hashes_into_previous_set`, and the
    guide line-count/banned-terms tests.
- dan-jefferies passes: (1) re-read staged diff — one finding while writing,
  first `_guide_kind` probe passed a Path instead of str (signature is
  `_guide_kind(existing: str | None, guide: bytes)`); corrected, not a code
  issue. (2) criteria re-anchored to design 1.1/1.2/5.1–5.3/6 above. (3) smell
  sweep: docs+hash-entry change only, no runtime paths, no new helpers, no
  contract drift, no deps; agent-guide.md deliberately untouched.
- implemented: revert of c59a347 (3 files), committed 23233cf. verified:
  hash lineage, copy identity, managed-kind, row in shipped bytes, 489 + 71
  green. not verified: `test_update.py` (lives in T1's worktree, absent here).
  deferred: none. discovered-but-not-fixed: none.
- Confidence: high — the fix is the exact inverse of the breaking revert and
  every sub-claim of the finding is corroborated by two routes plus green gates.
- No push, no merge, no deploy, no live-database contact.
