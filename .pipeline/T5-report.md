# T5 report — Docs: spec copy, agent guides, README config row, changelog, managed-guide upgrade

- status: DONE (not blocked)
- taskWorktree: /home/ingmar/worktrees/ptest/cc-post-test-stall/ptest-T5
- taskBranch: feature/post-test-stall-T5
- commit: 4fb5935 ("T5: post-test-stall docs, managed-guide upgrade recognition")
- base: 3ab75e8

## What was done (all acceptance items)

1. **Spec copy**: `docs/specs/2026-10-05-post-test-stall.md` is a byte copy
   (`cp`, `cmp` clean, 219 lines) of
   `/home/ingmar/worktrees/ptest/specs/2026-10-05-post-test-stall.md`.
2. **Guide row**: `docs/ptest-agent.md` and
   `src/ptest/resources/repository-agent-guide.md` gained one output-table
   `post-test-stall` row after the `execution-timeout` row (meaning: tests
   finished, processes hung in teardown/shutdown, exit 70, stack dumps
   above; action: rerun once alone, report a repeat with the stack dump,
   never edit tests to dodge it, lasting change `[runner] stall_timeout`,
   seconds, default 120, 0 disables). `cp` + `cmp` clean (byte-identical).
   Guide is exactly 100 lines: +1 row line, −1 by joining the two-line
   Monorepo opener into one line (no asserted phrase touched).
3. **agent-guide.md**: one appended paragraph with the same meaning/action
   (exit 70, rerun once alone, report repeat with dump, `[runner]
   stall_timeout`, 0 disables). All existing assertions hold.
4. **README**: "Reading the output" `post-test-stall` row (exit 70, rerun
   once alone, lasting change `[runner] stall_timeout`, default 120,
   0 disables, 0 or 10..86400, no CLI flag) + the exact commented
   `[runner]` example line
   `# stall_timeout = 120  # pytest: end a run whose tests finished but whose processes stay idle; 0 disables`.
5. **Changelog**: new top `## Unreleased` (G1 dumps + G2 stall detection,
   new key and code, no CLI flag). No version bump.
6. **Managed-guide upgrade**: `_PREVIOUS_GUIDE_SHA256S` gains
   `87464290…` with the exact design comment
   `# 04d968c (0.4.8-0.4.9): guide before the post-test-stall row.`
   (hash corroborated by `sha256sum` of the pre-change guide before
   editing — matched). New fixture
   `tests/ng/fixtures/previous-guides/3ab75e8-ptest-agent.md` is a byte
   copy of the 0.4.9 guide (`cmp` clean). New twin test
   `test_released_3ab75e8_guide_upgrades_in_place`: fixture hashes to the
   registered digest, `_guide_kind` → "previous", `apply` rewrites to
   current bytes, edited copy still raises already-exists.
   `test_every_shipped_guide_version_hashes_into_previous_set` green.
7. **No migration generated** (none exists in this repo).

## TDD evidence

- Red: after adding the two tests, `ptest
  tests/ng/test_agent_rules.py tests/ng/test_resources.py` →
  `3 failed, 69 passed` (the new twin, the new docs test, and the
  existing structured-guide test via the extended row list). Exit 1.
- Green: after implementation, `ptest tests/ng/test_agent_rules.py
  tests/ng/test_resources.py tests/ng/test_init_changed.py
  tests/ng/test_uninstall.py` →
  `ptest: passed · 147 tests · 1m1s` (exit 0). Covers the new tests, the
  guide line-count/banned-term tests, the hash-set walk, and the
  fixture-based uninstall twin. (The run printed `unknown-input: the
  scoped run ran on a dirty source tree` — transient uncommitted-tree
  artifact; verdict `passed`, tree committed clean at 4fb5935.)

## dan-jefferies passes

1. Re-read full diff: one finding while writing — the Monorepo join had
   to preserve the asserted `` `ptest <project>/`` substring; verified
   intact. No other findings; touched set == owned set (10 files), no
   `.pipeline/` or local-only files staged.
2. Acceptance re-anchored to design §T5: spec verbatim ✓; row meaning +
   exit-70 + action + stall_timeout/0-disables ✓; byte-identical copies
   ✓; ≤100 lines (exactly 100) ✓; no "xdist"/"fingerprint"/"expected:"
   (grep 0; also clean of cheap-model/graphify/fast-forward-lowercase/
   api//baseline/coverage/automatic/--changed per test_init_changed) ✓;
   every asserted phrase kept (suites green) ✓; agent-guide paragraph ✓;
   README output row + exact commented config line ✓; Unreleased, no
   bump ✓; hash entry + comment verbatim ✓; fixture + twin test ✓.
3. Smell sweep: docs + hash-entry change only — no runtime paths, no new
   helpers, no contract drift (contracts.py untouched; barrier content
   already on base), no deps, no shims. No pre-existing-failure claims
   made. No push/merge/deploy; no live-database contact.

## Integration notes

- Seam (not a blocker): guide/README/changelog quote T1–T4 frozen
  strings (guard message `tests finished but runner processes stayed
  idle for {N:g}s without exiting`, stderr header `ptest: stack dumps
  (<n> processes) — <reason>`, truncation-note prefixes). If T3/T4 ship
  different bytes, run a grep cross-check after merge.
- `test_every_shipped_guide_version_hashes_into_previous_set` walks the
  resource's git log: after merge it will additionally require whatever
  T2's contracts/config work does to the guide — T2 owns no guide file,
  so no conflict expected.

## Honest closing

- implemented: all 10 file changes, committed as 4fb5935.
- verified: spec/fixture/guide byte-identity, 100-line count,
  banned-term scans, hash corroboration, red→green, 147 scoped tests
  green.
- not verified: `ptest --full` (final integrated gate, run once by
  someone else per machine rules).
- deferred: none. discovered-but-not-fixed: none.
- Confidence: high — every acceptance item is byte-checked or
  suite-guarded, and the diff touches nothing outside the owned set.
