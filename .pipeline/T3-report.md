# T3 report — dependency store, ingest, deselect writer, uninstall

status: DONE
taskWorktree: /home/ingmar/worktrees/ptest/cc-dynamic-selection/ptest-T3
taskBranch: feature/dynamic-selection-T3
taskCommit: 50909be
baseCommit: f478189
filesOwned:
- src/ptest/selection_store.py (new, 1373 lines)
- src/ptest/selection_ingest.py (new, 665 lines)
- src/ptest/uninstall.py (+94)
- tests/ng/test_selection_store.py (new, 30 tests)
- tests/ng/test_selection_ingest.py (new, 22 tests)
- tests/ng/test_uninstall.py (+4 tests)

verification:
- command: `ptest tests/ng/test_selection_store.py tests/ng/test_selection_ingest.py tests/ng/test_uninstall.py` from the task worktree root
- result: 123 passed, exit 0 (run 2026-10-07, after final edit)
- TDD: both new suites errored on collection before implementation
  (ImportError: no module named selection_store/selection_ingest), then
  iterated red→green; no test was weakened (three test-expectation bugs
  of mine were corrected to the frozen semantics: dense snapshot ids,
  run-collapse under shared node ids, FK-valid LRU fixture).
- A5: synthetic 20k-node store (150 funcs/node) updates in ~6.5 s and
  occupies 9.3 MiB on disk vs the 64 MiB cap (measured 2026-10-07).
- No migration generated. No push, no merge, no deploy. `.pipeline/`
  unstaged (only explicit owned paths committed).

## Acceptance anchoring (design T3 checkboxes)

- Store basics: update→snapshot round trip with dense ids and
  `selection_ids` arrays (store.py:714 snapshot;
  test_update_snapshot_round_trip); replace-only-ran + fixture
  replacement (test_update_replaces_only_ran_nodes,
  test_fixture_records_replaced_per_run); per-run baselines,
  unreferenced-run prune (test_unreferenced_runs_pruned); two-full-
  inventory prune (test_full_prunes_nodes_absent_from_two_inventories);
  mark_outcomes/invalidate/demote/record_audit/note_inactive/meta
  (store.py:1281-1373; dedicated tests).
- Concurrency: two threads × 25 updates, distinct + shared node, no
  sleeps; BEGIN IMMEDIATE + busy_timeout, last-writer-wins, 51 nodes /
  50 runs coherent (test_concurrent_updates_from_two_connections).
- Private files: symlinked db/dir, mode 0644, monkeypatched foreign
  uid, hard link → `unsafe-path`; create=False never creates; hot
  journal recovers via storage.open_database + support.leave_hot_journal;
  garbage bytes → `coordinator-corrupt`, then remove_store + open
  rebuilds empty (store.py:482 open_store; tests).
- Size: 20k nodes ≤ cap (9.3 MiB); monkeypatched-target eviction is
  oldest-first, never fails, and compacts the file below the
  accumulated peak (post-commit VACUUM, store.py `_vacuum`); cache LRU
  prune (ancient + unreferenced); capacity-exceeded after evict-and-
  retry via pinned max_page_count (test_capacity_exceeded…).
- N7: project A records invisible from project B (test_project_isolation_n7).
- Never stored: db bytes scanned for source/data sentinels and their
  unkeyed sha256 — absent (test_never_stores_contents_or_unkeyed_digests).
- Ingest (ingest.py:442 read_run): serial + xdist merges; completeness
  (missing worker, count mismatch, overflow, dup/extra controller);
  10 malformed mutations + truncated JSON + hostile paths (→ opaque,
  never dropped) + symlink/foreign/mode/nlink/oversize refusals — all
  incomplete-with-note, never raising, nodes == {} on rejected files;
  qualname normalisation (`<module>`/`<lambda>` → module body);
  duplicate-node worst-outcome + union; other-report ignored; scan
  bounded at 4096 via islice (test_dir_scan_bounded).
- Deselect writer (ingest.py:550): O_EXCL|O_NOFOLLOW|O_CLOEXEC 0600,
  exact JSON, ValueError on unsafe ids, brackets/`::`/unicode/newline
  round trip; cleanup removes only own files, never through links,
  idempotent (ingest.py:646).
- Uninstall: REMOVE kind "selection-store" entries for root + each
  declared child with a valid project id (uninstall.py:512-620);
  dry-run clean; apply identity-checked; symlink/foreign SKIPPED;
  all 71 uninstall tests green, JSON shape untouched.

## Integration notes (seams for T5/T6)

1. BARRIER ABSENT — nothing to branch from: no worktree in this run
   has the section-12 contracts barrier (chain + T2/T5/T6 all sit at
   f478189 with unmodified contracts.py). Per the task brief I declared
   the frozen shapes locally instead of patching contracts.py:
   selection_store.py lines 60-300 alias every SELECTION_*/dataclass/
   helper name to `contracts` when present, else define the verbatim
   frozen shape; selection_ingest.py reuses those names from
   selection_store. When T5 lands the barrier, the aliases take over
   with no edits here. T3 imports only contracts/files/storage
   (never T1/T2/T4).
2. `write_deselect_binding` raises ValueError (bad run id, unsafe id,
   over-limit, oversize) and OSError (exists/symlink/I/O) — following
   design 2.6 + the task brief, not the "raises C.Problem" line in the
   2.5 signature block. T5 operations should catch Exception around it
   (a write failure still runs everything).
3. Duplicate-node merge ranks "unknown" below "skipped" (a definitive
   report beats absence of one); ingest.py:80 `_SEVERITY`. Flag if T5
   assumes otherwise.
4. A `None` normalised qualname becomes a module-body (modules-set)
   entry, not a `(path, "")` function id (ingest.py:354-359).
5. `update()` keys nodes by `value.nodeid` (mapping keys ignored) —
   T5 must keep them consistent (they are, by construction).
6. `PRAGMA max_page_count` is per-connection (verified empirically:
   it does not persist in the file header), so capacity tests must pin
   it on the store's own connection.
7. Eviction needs post-commit VACUUM, not just in-transaction
   incremental_vacuum: row deletes leave freeblocks, not free pages,
   so the file never shrinks otherwise (found by re-reading my own
   diff; proven by the peak-relative shrink assertion).

## Honest closing

- implemented: all T3 acceptance rows above; 6 owned files committed.
- verified: 123/123 owned tests green in one run; A5 size measured;
  eviction shrink, hot-journal recovery, corruption rebuild observed.
- not verified: cross-task seams that need the merged branch — T5's
  `after_run` calling read_run/update, T4's real bridge files against
  read_run (T4 owns that cross-check, gated on my module existing),
  `ptest --full` (integrated gate, explicitly out of scope for tasks).
- deferred: none (file list in the brief is fully delivered).

## Fix report — review findings round 2 (2026-10-07, dan-jefferies-agent)

Worktree: `/home/ingmar/worktrees/ptest/cc-dynamic-selection/ptest-T3`
(unchanged). Owned files touched this round:

- src/ptest/selection_store.py (fixes only, no API changes)
- tests/ng/test_selection_store.py (+4 regression tests)

Verification: `ptest tests/ng/test_selection_store.py
tests/ng/test_selection_ingest.py tests/ng/test_uninstall.py` from the
task worktree root → 127 passed (was 123; +4 new), exit 0. Pre-fix
sensitivity proven per test in a scratch worktree at HEAD (0bccd07)
with the new test file copied over: all 3 behavior tests failed there
(see below), then the scratch worktree was removed.

1. Eviction wipeout (HIGH). `_db_size` now measures
   `(page_count - freelist_count) * page_size`, and both
   `PRAGMA incremental_vacuum` call sites are drained with `.fetchall()`
   (store.py `_db_size`, `_evict_to_target`, `_evict_oldest`). New test
   `test_eviction_removes_only_oldest_run` measures one run's growth and
   sets the target to current + growth − 3 pages, then asserts exactly
   the oldest run is gone (450 nodes over the 3 survivors). Pre-fix this
   test FAILED (only the newest run survived); post-fix it passes. Note:
   a first version of this test used a half-run allowance and PASSED on
   unfixed code — the allowance exceeded what the buggy loop sheds per
   eviction, so it could not catch the bug; the growth-minus-3-pages
   form was verified to fail pre-fix before keeping it.
2. Concurrent first-create classified corrupt (HIGH). `_check_schema`
   now takes `BEGIN IMMEDIATE` (bounded retry via `_begin_immediate`)
   and re-reads `sqlite_master` under the lock before creating or
   declaring corruption; `_create_schema` uses `CREATE TABLE IF NOT
   EXISTS` / `INSERT OR IGNORE`; the fast path also requires the
   `schema_version` row (a creator's tables land before its kv rows),
   falling through to the locked recheck otherwise; COMMIT failure maps
   to a typed Problem so `open_store` still raises `C.Problem` only. New
   test `test_concurrent_create_from_two_connections` races two
   barrier-aligned `open_store(create=True)` over 25 fresh projects and
   asserts no `coordinator-corrupt` and a coherent store. Pre-fix this
   test FAILED; post-fix it passes (also re-run in the full suite).
3. Lock timeout loses an upsert as raw OperationalError (HIGH).
   `_apply_update` (and `_evict_oldest`, `_check_schema`) take the lock
   through `_begin_immediate`: transient busy/locked failures retry to a
   60 s budget (one busy_timeout wait cannot cover a ~6.5 s big update +
   VACUUM). `update` maps leftovers via `_map_update_error`: transient →
   retryable `coordinator-unavailable`, I/O → `state-unavailable`,
   anything else re-raised unchanged (FULL path untouched). New tests:
   `test_concurrent_update_while_lock_held` (second connection updates
   while a raw connection holds RESERVED; busy_timeout shrunk to 50 ms
   and the lock held 0.5 s — 10× past the timeout — instead of a literal
   >2 s hold, per the no-long-timeouts rule; the retry path is identical)
   and `test_update_error_mapping_is_typed` (lock→unavailable/retryable,
   I/O→state-unavailable, unknown error re-raised). Both FAILED pre-fix
   and pass post-fix; no raw `sqlite3.Error` escapes `update` for lock
   or I/O causes.

Honest closing (this round): implemented all three finding fixes;
verified 127/127 owned tests green plus pre-fix red on each behavior
test; not verified: `ptest --full` (integrated gate, out of scope);
deferred: none; discovered-but-not-fixed: none in owned files.
Confidence: high — each new test was observed red on unfixed code and
green on fixed code.
- discovered-but-not-fixed: none in owned files; out-of-scope smell:
  none worth filing.
- confidence: high — every acceptance row has a owning test seen red
  then green, and the three self-review passes each surfaced a real
  fix (unused imports, nodeid-key skew, missing VACUUM).
