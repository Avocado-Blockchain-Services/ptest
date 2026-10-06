# T3 report — guard: marker-armed CPU-idle stall detection and dump signal

- taskWorktree: /home/ingmar/worktrees/ptest/cc-post-test-stall/ptest-T3
- taskBranch: feature/post-test-stall-T3
- base: 3ab75e8
- status: DONE (all acceptance criteria met; see deviations/notes in §5)
- commit: `855738f` T3: marker-armed CPU-idle stall detection and SIGWINCH dump signal in guard
- test commands (all from the worktree root, per machine rules): `ptest tests/ng/test_stall_window.py`, `ptest tests/ng/test_guard.py` (full file), `ptest tests/ng/test_guard.py -k "<selection>"`

## 1. What changed (owned files only, 7 files, +895/-7 vs base)

- NEW `src/ptest/stall.py` — pure idle-window logic, no clock, no I/O: `idle_threshold_s(window_s) = max(0.5, 0.02 * window_s)`; `IdleWindow(window_s).observe(now, sample)` over `(pid, create_time) -> CPU-seconds` samples. Per-key positive deltas accumulate, new keys add full value, vanished keys add nothing, `None` resets and reports busy. The retained history keeps the newest pre-cutoff sample as baseline (see §4 for why), plus an idle latch released by real progress or reset.
- `src/ptest/platform.py` — NEW `group_cpu_times(pgid, *, exclude_pid, limit=8192)`: per-member `user + system + children_user + children_system` keyed by `(pid, create_time)`; skips zombies/excluded pid/vanished races; returns `None` on any ambiguous/unreadable member or past-limit scan (caller treats `None` as busy).
- `src/ptest/guard.py`
  - Module globals `_STALL_POLL_S = 1.0` and `_DUMP_WAIT_S` (contracts `STALL_DUMP_WAIT_S` with a `1.0` getattr fallback while the shared-contracts barrier is absent), both read at call time.
  - `_State` gains `dump_requested` / `external_cancel`. `cancel()` (signal handler, control frame, unexpected-error path) sets `external_cancel`. `fail(problem, *, dump=False)` records the first problem, arms `dump_requested` only when first and no cancel pending, and closes the spawn with SIGTERM without marking external cancel. All existing single-arg `fail()` callers are behaviour-identical.
  - NEW `_StallWatcher` (marker lstat at most every `_STALL_POLL_S`; armed = lstat `S_ISREG` + `st_uid == getuid`; then CPU samples into `IdleWindow`), built only when phase == `execution`, `manifest.stall_timeout_s` is a positive number, and `prepared.report_path is not None`.
  - `_run_one` child loop: deadline check first (`fail(..., dump=True)`), then stall check (skipped once `cancel_signal` is set — first problem wins), then on cancel: `_dump_and_wait` (SIGWINCH via `_signal_group` + bounded `control.poll` wait interruptible by external cancel) only when a dump was requested, no external cancel exists, and a report binding exists; then the existing `_cancel_and_reap`.
  - `run_guard` final return treats `post-test-stall` like `execution-timeout` (not `_EXIT_PROTOCOL`). Frozen message: `tests finished but runner processes stayed idle for {stall_s:g}s without exiting`.
- NEW `tests/ng/test_stall_window.py` — 13 unit tests (threshold floor/fraction, span, sliding, None-reset, new/vanished/reused-pid members, threshold boundary, `group_cpu_times` live-child/exclude/unreadable/limit).
- `tests/ng/test_guard.py` — 17 new test items: stall kill with dump-before-SIGTERM ordering + no second attempt; group-wide child dumps; N1 unarmed survival; N2 armed-busy survival; symlink/FIFO/dir/other-report markers never arm; N9 deadline SIGWINCH without stall; D4/B9 no-report-path no-signal/no-wait; N8 control-cancel + SIGINT (130) no-signal/no-delay; mid-dump-wait cancel promptness; deadline-wins race; N5 forged-identity `_signal_group`; `fail`/`cancel` dump-request unit semantics. Plus `Harness.start` `stall_poll`/`dump_wait`/`stall_timeout` env knobs.
- NEW `tests/ng/fixtures/processes/stall_workload.py` — bridge simulator: `armed-idle`, `unarmed-idle`, `armed-busy` (socket-released CPU spin), `armed-idle-child` (same-group child with own dump file); O_EXCL|O_NOFOLLOW|O_CLOEXEC marker + dump creation, faulthandler SIGWINCH registration, blocking frame `stall_blocked_teardown` for dump identification, `.term` note recording `15` vs `released`.
- `tests/ng/fixtures/processes/guard_driver.py` — `GUARD_STALL_POLL` / `GUARD_DUMP_WAIT` global patches and a `GUARD_STALL_TIMEOUT` decode-time manifest injection seam (test-only; needed only until the contracts barrier lands — see §5).

No migration (none exists in this repo). No push/merge/deploy; no live databases touched.

## 2. Verification (all through `ptest` from the worktree root)

- New tests pre-change (TDD red): `ptest tests/ng/test_guard.py -k "stall or ..."` → **8 failed, 11 passed** (all 8 abuse tests failed: stall kill, child dumps, deadline-dump, mid-wait cancel, race-dump, both `fail` unit tests; the 11 passes are negative-preservation tests + the N5 unit test, green before and after as designed).
- `ptest tests/ng/test_stall_window.py` pre-implementation → collection error (`No module named 'ptest.stall'`).
- Post-change: `ptest tests/ng/test_stall_window.py` → **13 passed**; `ptest tests/ng/test_guard.py` (full file) → **112 passed, 0 skipped** (includes every pre-existing guard test with unchanged timing assertions); solo re-run of the busy test → **1 passed**.
- `graphify update .` run in the worktree (index refreshed; no repo files touched by it).
- Direct probes (throwaway, `/tmp`, not committed): watcher-vs-live-process traces that isolated the §4 eviction bug and confirmed the fix (fires ~1.0 s after arming; True/False alternation observed is cadence gating, not verdict flap).

## 3. Pass 1–3 self-review (dan-jefferies-agent)

- Pass 1 (re-read bytes): full `git diff` re-read. Found and fixed: an extra paren in the eviction edit (caught on re-read, verified by re-reading the hunk); a wrong SIGINT exit expectation in my own new test (143 → `128 + SIGINT`, caught by the pre-change red run). No TODO/FIXME/HACK in owned files. All `_State()` constructions are zero-arg (new fields defaulted, safe). Single `lstat`, no-follow, in guard.
- Pass 2 (re-anchor to design §T3): every acceptance checkbox maps to a passing test — stall kill (message byte-exact, 143, facts phase `execution`, dump-before-SIGTERM by content + `st_mtime_ns` order, `a002` absent), child group dumps, N1, N2, N6/N4 shapes (4 tests), N9, D4/B9 (5 s wait leaves no trace without a binding), N8 ×3, same-poll race (execution-timeout), N5, unit file, existing suite green. Frozen names/values used: `.done` suffix rule (delegates to `contracts.stall_marker_path` when the barrier lands), `_STALL_POLL_S = 1.0`, `_DUMP_WAIT_S = STALL_DUMP_WAIT_S`, SIGWINCH only via `_signal_group`, no guard SIGWINCH handler, per-attempt marker path, post-test-stall final-return handling.
- Pass 3 (smell sweep): no duplicated concepts (`group_cpu_times`/`IdleWindow`/`_StallWatcher` are new names; kill path reuses `_signal_group`/`_cancel_and_reap`); no new deps; no config changes; `fail()` keyword-only `dump` keeps all existing callers compatible; `group_cpu_times` full-table scan cost (~0.1 s on a loaded box) is within the mandated ≤1/s cadence. Tests kill the no-op: every abuse test failed pre-change; boundary unit tests discriminate idle/busy/reset. `audit-spec` was considered; the brief-mandated Pass 1–3 above already covers diff re-read + criteria anchoring + sweep, so a second audit loop was not run.

## 4. Root-cause notes (found during the work, fixed)

- **IdleWindow eviction bug (mine, fixed):** dropping every pre-cutoff history point shrinks the retained span below the window on every pass, so `span >= window` could only hold by float luck — the two real-process kill tests hung (10 s watchdog) while a luck-hit test passed. Fix: retain the newest pre-cutoff sample as baseline; measuring from it can only add pre-window progress, delaying the verdict by at most one sample (safe direction for a kill). Unit expectations updated to the pinned-baseline semantics (burst anchors the window until its sample ages out).
- **Busy-test load flake (hardened, not weakened):** at 8 cores / load ~11 a single-thread spinner (fair share ~0.7 core) sat too close to the 0.5 s-per-1.0 s threshold and took one false stall in the suite run. The test still uses the design's 1.0 s setup everywhere except N2, which now uses a 2.0 s window: the floor threshold stays 0.5 s, so the spinner needs only a quarter core on average. Assertions unchanged (survive, release, exit 0, no problem). Extra spinner threads were rejected (GIL: no added CPU).

## 5. Integration notes for the orchestrator (barrier absent in this worktree)

- The shared-contracts barrier (§9 of design) is **not present** in any worktree (checked chain/T2/T4): no `stall_timeout_s`, no `STALL_*` constants, no `post-test-stall` reason code. My diff touches none of T2's files, so it stays mechanically clean, via two forward-compatible seams: (a) `guard.py` resolves `STALL_DUMP_WAIT_S` / `stall_marker_path` / `manifest.stall_timeout_s` with `getattr` fallbacks equal to the frozen values (real path activates automatically once T2 merges); (b) `guard_driver.py` injects `stall_timeout_s` at manifest-decode time from `GUARD_STALL_TIMEOUT` because `encode_launch_manifest` cannot carry it yet — after the merge, tests can set the field directly and this wrapper becomes inert (it object-sets the same field). `Problem("post-test-stall", ...)` needs no REASON_CODES membership on the guard path (control-frame payloads are opaque dicts).
- Claimed-vs-shipped delta: none against the T3 design section. Deferred: nothing. Discovered-but-not-fixed: `group_cpu_times` walks the full process table per sample (~0.1 s loaded) — mandated cadence, no action.
- Confidence: high — every acceptance test is real-process against the real guard, red-before/green-after, plus full-file green (112) proving no existing-timing drift.

## Structured fields

- taskWorktree: /home/ingmar/worktrees/ptest/cc-post-test-stall/ptest-T3
- taskBranch: feature/post-test-stall-T3
- status: DONE
- commit: 855738f
- tests: tests/ng/test_stall_window.py 13 passed; tests/ng/test_guard.py 112 passed (full file, 0 skipped)

---

# T3 fix report (follow-up, 2026-10-06) — reviewer findings resolved

- taskWorktree: /home/ingmar/worktrees/ptest/cc-post-test-stall/ptest-T3
- taskBranch: feature/post-test-stall-T3
- base: 855738f (prior T3 commit); `src/ptest/guard.py` untouched by this fix (verified: `git diff --stat src/ptest/guard.py` empty at commit time)
- owned files (2): `tests/ng/fixtures/processes/stall_workload.py` (spinner non-blocking), `tests/ng/test_guard.py` (N2 window, race rewrite, multi-attempt test)
- status: DONE
- commit: (see `git log` on the task branch; message `T3 fix: non-blocking busy spinner, deterministic race test, multi-attempt marker test`)
- test commands (all from the worktree root, per machine rules): `ptest tests/ng/test_guard.py -k "<selection>"` per change, then `ptest tests/ng/test_guard.py tests/ng/test_stall_window.py` (final green)

## F1. Busy-spinner fixture fixed at the root cause; N2 back to the mandated 1.0 s window

- Cause confirmed as reported: `stall_cpu_spin_until_released` spun 50 ms then blocked 50 ms in `peer.recv(1)` with `settimeout(0.05)`, idling ≥50% of every pass. The 2.0 s window only widened the margin instead of fixing the fixture.
- Fix (`stall_workload.py`): `peer.setblocking(False)`; release polls now raise `BlockingIOError` when no byte is waiting. No other use of that socket exists in the workload, and `stall_blocked_teardown` (blocking `recv`) is untouched.
- Isolated duty measurements through the shipped function on a socketpair, released after 2.0 s (`uv run python /tmp/spin_probe.py`, throwaway, not committed): before `wall 2.0 cpu 0.75` (per the finding); after: `0.97`, `0.88` on repeat runs (one early run read `0.70` while the box sat at load 14 on 8 cores — a pure-spin control showed `0.88` under the same load, so the residual gap is scheduling noise, not the fixture). Even at 2/3 core share the busy workload now clears the 0.5 s-per-1.0 s floor with margin.
- N2 (`test_stall_armed_busy_survives_window`) returned to `stall_timeout=1.0` with the 2.5 s survival assertion, matching every other real-process stall test and the design's acceptance setup. Passed via `ptest` (1 passed in 10.56 s).

## F2. Same-poll race test rewritten as a deterministic unit-level `_run_one` test

- The old `test_deadline_wins_same_poll_race` (1 s deadline vs 5.0 s window) is deleted: both verdicts could never be due in one pass, so it could not distinguish deadline-first from stall-first and never counted dumps.
- New test (same name, `monkeypatch`-only, no `harness`): frozen clock + advancing fake `control.poll`, stub child (alive for the verdict pass, reaped after), stub `_stall_watcher_for` returning a watcher whose `check()` is always due (`stall_s=5.0`), stubbed `_signal_group` recording signals, real `_dump_and_wait`. It asserts: `watcher.checks == 0` (stall check skipped once the deadline owns the timeline), exactly one `fail()` call with `("execution-timeout", True)`, signals sent `== [SIGWINCH, SIGTERM]` (one dump, one kill, in order), `result == 0`, and one `runner-facts` frame carrying `execution-timeout`.
- Self-caught test bug during the work: the first version stubbed `prepared` without `argv`/`cwd`/`env_updates`, so `_run_one` raised `AttributeError` before any verdict (seen in the first M_f run output). Fixed by completing the stub; the test was then observed green on clean code (4 passed together with the multi-attempt and `fail`-semantics unit tests) before any mutation was (re-)applied.
- Mutation evidence (each: edit `src/ptest/guard.py`, run, exact revert, `git diff` confirms pristine):
  - M_f (stall check moved before the deadline check, guard kept): test fails with `assert 1 == 0` on `watcher.checks` (stall consulted first; problem would be `post-test-stall`).
  - M_g (order kept, `state.cancel_signal is None` guard dropped on the stall check): test fails with `assert 1 == 0` on `watcher.checks` (unguarded second consult); the `fail_calls == [("execution-timeout", True)]` assertion is the second tripwire for the same regression.

## F3. Abuse evidence completed: marker-shape mutations + the missing multi-attempt test

- New `test_stall_first_attempt_marker_does_not_arm_second`: a001 `armed-idle` (own report path) is released to exit 0, then an idle a002 with its own report path and no marker must survive a 2.0 s window; asserts both facts (`a001`/`a002`, exit 0, no problems) and `released` term note. Uses the Harness `later`-style two-attempt manifest with per-attempt `report_path`s (the `auto_decide` path answers the a002 decision).
- Mutation evidence for the marker-shape tests (same edit/run/revert discipline):
  - M_a (`_marker_armed` → `marker.exists()`, i.e. pre-change existence-only behavior): `foreign_marker[symlink/fifo/dir]` + `symlink_not_written_through` all fail (4 failed, 1 passed); `other_report_marker` still passes, as expected — it tests path binding, not shape.
  - M_b (`os.lstat` → `os.stat`): exactly the 2 symlink tests fail; fifo/dir pass — `lstat` is load-bearing for symlinks.
  - M_c (`S_ISREG` check dropped, uid kept): all 4 shape tests fail.
  - M_d (uid check dropped, `S_ISREG` kept): all 5 pass. Reported honestly: uid-check removal is not observable in this suite (every fixture marker is same-uid; a foreign-uid fixture would need `chown`, unavailable to the test user). The uid conjunct remains defended by code inspection, not by a red run.
  - M_e (watcher cached across attempts — the exact feared regression, arm state moved out of `_run_one`): the new multi-attempt test fails because a002 is killed mid-window (`psutil.NoSuchProcess` inside `_assert_alive`; a001's stale marker arms the carried-over watcher and the idle a002 reads as stalled).

## Verification (all through `ptest` from the worktree root)

- `ptest tests/ng/test_guard.py tests/ng/test_stall_window.py` → **126 passed** (113 guard incl. the rewritten race unit test and the new multi-attempt test; 13 window), 0 failures.
- `src/ptest/guard.py` has zero diff vs the prior T3 commit — production behavior is unchanged; this fix is fixture + test only, as the findings required.

## Pass 1–3 self-review (dan-jefferies-agent)

- Pass 1 (re-read bytes): full `git diff` re-read of both owned files. Found: nothing left over (all `TEMP MUTATION` markers reverted; `socket` import still used by the workload; `emitted = []` placed after the `_Control` class but before first use — closure resolves correctly). The `prepared` stub gap (F2) was caught by an actual red run, not by re-reading.
- Pass 2 (re-anchor to design §T3 + abuse table): `stall_timeout_s = 1.0` everywhere incl. N2; same-poll criterion now asserts one problem / one dump signal / one kill signal with order pinned; `a001 marker vs a002` has a real multi-attempt test; every marker-shape test has a recorded red run except the uid conjunct (M_d, disclosed above).
- Pass 3 (smell sweep): no new production helpers, no new deps, no config changes; test fakes mirror existing file conventions (`SimpleNamespace` stubs, `monkeypatch.setattr`); `_Child.calls` is a per-test class attribute (defined inside the test, single use — cannot leak across tests); no TODO/FIXME/HACK added.
- Claimed-vs-shipped delta: none — all three findings fixed. Deferred: nothing. Discovered-but-not-fixed: nothing new (the `group_cpu_times` scan-cost note from the prior report stands).
- Confidence: high — every changed behavior has a green run on clean code and a recorded red run under the matching mutation, with `guard.py` byte-identical to the reviewed T3 commit.

## Structured fields (fix)

- taskWorktree: /home/ingmar/worktrees/ptest/cc-post-test-stall/ptest-T3
- taskBranch: feature/post-test-stall-T3
- status: DONE
- tests: tests/ng/test_guard.py + tests/ng/test_stall_window.py → 126 passed
- production diff: none (tests + fixture only)
