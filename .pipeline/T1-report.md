# T1 report — offline doctor static packet path

Date: 2026-09-29. Task worktree: `/home/ingmar/worktrees/ptest/cc-doctor-offline/ptest-T1`,
branch `feature/doctor-offline-T1`, commit `edc3246` (parent `09f6f40`).
Base for before-numbers: `985d43e` (Release 0.3.7), via a detached worktree with its own
`uv sync` (removed after measurement).

## Outcome

Implemented and verified. Offline doctor (`doctor --offline`, offline `--json`, init's
declined-review fallback) now builds static evidence packets that admit only core and
marker excerpts and project every other admission outcome from file sizes. The static
path never calls `_candidate_text_pool`, `review_evidence.rank_candidates`,
`review_evidence.item_source_chains` or `_resolve_tier4`. The online `build_packets`
path is behaviorally unchanged.

## Files changed (owned set only)

- `src/ptest/agent_assessment.py` — `StaticPackets`, `build_static_packets`,
  `_deadline_packet`, `_project_static_admission`, `packet_has_evidence`, and
  `EvidencePacket._projected_admitted` (default 0, excluded from packet identity);
  `_build_one_packet` gains keyword-only `static=False, on_child=None` (byte-identical
  behavior when `static=False`).
- `src/ptest/cli.py` — `_OFFLINE_DEADLINE_MESSAGE`, `_offline_progress`,
  `_offline_assessment_parts` / `_doctor_offline_assessment_json` deadline+progress
  plumbing, `doctor --offline -q | --quiet` grammar (D3a), and the two
  `packet_has_evidence` call sites in `_assessment_limitations` (behavior-neutral online).
- `src/ptest/doctor.py` — no edit needed (file counts come from the packet walk).
- NEW `tests/ng/test_doctor_offline_light.py` — 26 tests.
- NEW `scripts/bench_doctor_offline.py` — deterministic ~1000-file timing harness (not a test).

## Verification (all through ptest; no wall-clock assertions)

- `ptest --workers 2 --queue-timeout 1800 tests/ng/test_doctor_offline_light.py` →
  **26 passed**.
- Scoped set `test_doctor_offline_light.py test_agent_doctor_acceptance.py
  test_doctor_grid.py test_cli.py test_agent_assessment.py test_init.py test_help.py
  test_doctor_fix.py` → **745 passed, 1 failed**, where the single failure was a bug in
  my own new test (progress fixture contained 2 files, asserted "1 file"); after fixing
  the fixture the light file re-ran green (26 passed). All pre-existing tests passed
  unmodified. Two later source tweaks (quiet-check order per D3a, narrowing an
  `except` clause) cannot affect existing tests: `-q/--quiet` did not exist before, and
  the narrowed clause only handles `isatty()` failures.
- TDD: the new file was written first and run against unmodified code (failed on the
  missing `build_static_packets`; negative-contract tests fail on the old path because
  it calls the forbidden functions). Three implementation bugs found by the tests were
  fixed: preamble checkpoints defeating graceful expiry, an unsafe-scope test shape
  rejected too early by `inspect_workspace`, and the fixture bug above.

## Benchmark (scripts/bench_doctor_offline.py, 1007 files, interleaved runs)

| run | T1 offline (s) | T1 offline-json (s) | base offline (s) | base offline-json (s) |
|---|---|---|---|---|
| first | 0.740 | 0.491 | 11.322 | 8.865 |
| i1 | 2.807 | 1.500 | 12.917 | 14.094 |
| i2 | 0.722 | 2.646 | 19.484 | 6.273 |
| i3 | 0.651 | 0.732 | 13.644 | 13.182 |
| min | **0.651** | **0.732** | **12.917** | **6.273** |

Min-of-interleaved speedup on shared (loaded) hardware: grid ~20x, JSON ~9x. Single-shot
timings are noisy under sibling-suite load; interleaving balances it. Raw series kept
above for honesty.

## Acceptance notes

- Identity holds under the D4 exactness condition: JSON equal after replacing
  `children[*].packet_sha256`, grid equal after masking the duration token, child
  rows/findings/scores/execution/facts/limitations equal. `packet_sha256` always differs
  by design (the old value commits to ranked late excerpts plus the pool inventory
  digest, which requirement 1 forbids building); offline publication is always `skipped`
  so nothing compares it with an online identity.
- Deadline: `_REVIEW_TOTAL_TIMEOUT_S` (1800 s) reused, no new constant. Expiry yields
  empty packets, all-unknown rows, score percent 0, and the deadline `partial-evidence`
  limitation per child and top level (`[:64]` kept, no new limitation code).
- Progress: one `ptest: doctor: inspecting <child> · N files` stderr line per walked
  child on a TTY (`.` rendered as repo name, singular handled, labels sanitized,
  `OSError` swallowed); suppressed by `-q`, never on `--json` stdout.
- No database migration (none exists). No push, merge, deploy, or live-database contact.

## Integration notes (seams for the chain)

- `help.py` NOT edited: the T1 owned-file set excludes it while design D3a assigns the
  doctor-topic help text (`[-q | --quiet]` syntax line + Notes sentence) to T1. Parser,
  behavior and tests are done; the two-line help text still needs applying, else the
  `main(("help","doctor"))` assertion in design item 6 has nothing to hold. No help
  assertion was added to the T1 test file for this reason.
- `review_evidence.py` and `config.py` untouched (T2/T3 owned). The static path only
  relies on their frozen current behavior (empty bound chains select no sources).
- Brief-vs-design: the task brief sketches `_project_static_admission(...) -> None`;
  the authoritative design (D2/D2a) specifies `-> int` feeding the frozen
  `EvidencePacket._projected_admitted` field plus `packet_has_evidence`. Implemented
  per design.

## Structured fields

- task: T1 offline doctor light path
- taskWorktree: /home/ingmar/worktrees/ptest/cc-doctor-offline/ptest-T1
- taskBranch: feature/doctor-offline-T1
- commit: edc3246
- status: done
- tests: 26 passed (new file); 745 passed on scoped set (1 pre-fix failure was the author's own test bug, fixed)
- benchBefore: {"offline": 12.917, "offline-json": 6.273} (min of 3 interleaved, 1007 files, base 985d43e)
- benchAfter: {"offline": 0.651, "offline-json": 0.732} (min of 3 interleaved, 1007 files)
