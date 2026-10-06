# Spec: post-test stall detection and stack dumps before kills (ptest 0.4.10)

Date: 2026-10-05. Status: user-approved direction ("go ahead"). Copy this file
into the branch as `docs/specs/2026-10-05-post-test-stall.md`.

## Problem (observed, not reproduced)

On 2026-10-05 a downstream monorepo (persea, child `services/control-plane`,
843 tests, pytest-xdist `-n 8`, `timeout_func_only = true`) hung twice under
ptest 0.4.9 *after its tests had finished*: the xdist controller and all 8
workers sat idle in futex waits. The run held its queue slots, so every other
run on the machine queued behind it until an operator killed it by hand.

- The cause is unknown. A direct pytest run at the same commit, on an idle
  machine, finished in 93 s. That does not prove the hang is in ptest: the hangs
  happened under contention (about 4 concurrent runs, load about 8), and
  fixture teardown has no per-test time limit there. Nothing in the evidence
  shows which code held the lock.
- ptest's only bound today is the compound deadline. With no configured
  timeout, that is history × 3 or tests × 0.5 s, roughly 7–15 minutes for that
  suite. The guard has no signal that the tests are done, because the bridge
  writes its report only at session end, and a teardown hang never reaches it.

We cannot reliably reproduce it, and the machine's ptest queue is in heavy use,
so this change must (1) make the next natural hang diagnose itself and (2) free
the queue quickly once tests are provably done, without guessing a root cause.

## Goals

G1. **Stack dumps before any guard kill.** When the guard is about to kill an
    execution attempt because of the compound/attempt deadline or a post-test
    stall, every Python process of that attempt's pytest run (the xdist
    controller and each worker, or the single serial process) first writes
    all-thread Python stacks. ptest then prints them on stderr after the run,
    bounded and escaped.

G2. **Post-test stall detection (pytest runner only).** Once the bridge has
    proved that the tests are finished, and the attempt's process group then
    stays CPU-idle for `stall_timeout` seconds, the guard dumps stacks and kills
    the group. The run then ends **incomplete (exit 70)** with the new problem
    code `post-test-stall`, never as a pass.

## Definitions

- **Tests finished (arm condition)**: the bridge on the controller (the xdist
  controller, or the only process when serial) observed EITHER
  (a) a final outcome for every selected/collected test item: a call-phase
  report, or a setup-phase failure or skip that prevents call; teardown reports
  are NOT required, because a hang in the last item's or a session fixture's
  teardown must still arm; OR
  (b) the native test loop returned (`pytest_runtestloop` finished: covers
  `-x`/`--maxfail`, interrupts, and hangs in sessionfinish, unconfigure or
  interpreter shutdown).
  On arming, the bridge creates exactly one marker file. It never arms during
  collection or before any test ran, and a collection error with zero items
  arms only through (b).
- **Marker**: a file whose name derives from the attempt's bound native report
  path (for example `<report name>.done`) in the same private reports directory
  (0700, owned by the user, no symlinks). It is created with O_CREAT|O_EXCL|
  O_NOFOLLOW at mode 0600. Its existence is the whole signal: the guard never
  parses its content (an empty file or a tiny fixed token).
- **CPU-idle**: summed user+system CPU time of every live member of the
  attempt's process group, excluding the guard itself, grows by less than
  `max(0.5 s, 2% × window)` over a sliding window of `stall_timeout` seconds.
  Sampled at most once per second. A member whose CPU times cannot be read is
  treated as busy, never as idle (fail-safe: no kill on missing data).
- **stall_timeout**: new optional `[runner] stall_timeout` in `.ptest.toml`,
  in seconds. Default 120 when absent. `0` disables stall detection (dumps
  before deadline kills still happen). Valid values are 0 or 10..86400;
  anything else is a config error (exit 2) with the existing invalid-config
  shape. It is rendered by config render only when set, like `timeout`. There
  is no CLI flag: the agent rules forbid adding timing flags.

## Behaviour

B1. The guard checks for the marker in its existing poll loop (stat at most
    every 1 s). Arming starts the idle window. Detection fires only while the
    attempt's direct child is still alive.
B2. On stall, the guard records `post-test-stall` (first problem wins, as with
    `state.fail`), sends the dump signal (B4), waits at most 1.0 s (bounded and
    interruptible by a user cancel), then follows the existing
    `_cancel_and_reap` path (SIGTERM, grace, SIGKILL). No further attempts
    start after a stall.
B3. The compound/attempt deadline path also sends the dump signal and waits at
    most 1.0 s before its existing SIGTERM. A user or outside cancel
    (SIGINT/SIGTERM to ptest) sends NO dump signal and adds no delay.
B4. **Dump signal**: SIGWINCH sent to the attempt's process group through the
    existing `_signal_group` ownership checks. Its default action is *ignore*,
    so any group member that did not opt in (the `uv` launcher, shells, node,
    the guard itself) is unaffected. The bridge registers
    `faulthandler.register(SIGWINCH, file=<per-process dump file>,
    all_threads=True, chain=True)` in the controller before `pytest.main` and in
    every xdist worker at worker bootstrap. It adds NO pytest argv, ini options
    or `-p` flags (literal argv is preserved), and does not touch pytest's own
    faulthandler plugin or a user's `-p no:faulthandler`.
B5. **Dump files**: one per process, in the same private reports directory, with
    a name derived from the report name plus the PID (for example
    `<report name>.stack-<pid>`), opened O_CREAT|O_EXCL|O_NOFOLLOW|O_APPEND at
    mode 0600 and kept open for the process lifetime. A process that cannot
    create its file skips registration silently. A dump file must never break
    or alter a test run.
B6. **Printing**: after the attempt is reaped, operations reads that attempt's
    dump files (lstat-checked: regular file, owned by the user, no symlink) and
    prints a header line `ptest: stack dumps (<n> processes) — <reason>`, then
    each non-empty dump, controller first if identifiable, else by PID. Bounds:
    at most 32 files, at most 400 lines and 32 KiB per file, at most 128 KiB in
    total, with a truncation note. All dump text goes through
    `render.terminal_text` (or the equivalent escaping), so hostile file or
    function names cannot emit terminal control sequences. Output goes to
    stderr only, never into `--json` documents or history records. It is not
    suppressed by `-q` (errors still print).
B7. **Cleanup**: every run deletes that attempt's marker and dump files once
    the attempt is finalised, on every outcome (pass, fail, timeout, stall,
    cancel). Deletion is limited to names derived from this attempt's own
    report binding. Leftovers from a crashed ptest are covered by the existing
    reports-directory retention (extend it to these suffixes if it filters by
    suffix).
B8. **Verdict**: `post-test-stall` is a known problem code. It maps to status
    incomplete and exit 70. The end line reads, for example,
    `incomplete (exit 70): post-test-stall — tests finished but runner processes
    stayed idle for 120s without exiting; stack dumps above; rerun once alone,
    report a repeat`. Native test outcomes observed before the stall are NOT
    promoted to pass. A stall does NOT count as a deadline kill for dynamic
    deadline history (`kill_s`) and records no last-green or verified state.
B9. **Non-pytest runners** (vitest, simple): no marker and no stall detection.
    Whether the dump signal goes to their group is a design choice, but it must
    stay harmless (SIGWINCH is ignored by default). The behaviour is otherwise
    unchanged.
B10. Docs: add rows to `docs/ptest-agent.md` and the packaged agent guides
    (`src/ptest/resources/agent-guide.md`, `repository-agent-guide.md`) for
    `post-test-stall` (meaning: tests finished, processes hung in
    teardown/shutdown; action: rerun once alone; if it repeats, report it with
    the stack dump; never edit tests to dodge it; a lasting change is
    `[runner] stall_timeout`, 0 disables). Add a README config row and a
    changelog entry. If ptest has a managed-guide upgrade check (see "Recognize
    the previous managed guide on upgrade"), keep it consistent.

## Negative contracts (must NOT happen)

N1. A slow test still running (no final outcome yet, loop not returned) is never
    killed by stall detection, however idle it is.
N2. A CPU-busy group after the tests finished (coverage combine, report writing)
    is never killed by stall detection.
N3. A stalled run never reports pass or exit 0/1. It is always 70 /
    `post-test-stall`, even if every observed test passed.
N4. A marker belonging to another attempt, run, checkout or worktree can never
    arm this attempt. The path derives only from this attempt's own binding.
N5. The guard never signals or kills a process outside its own group (existing
    `_signal_group` invariants hold for SIGWINCH too).
N6. A symlinked, foreign-owned or non-regular marker or dump file is ignored
    (marker: does not arm) or refused (dump: not read, not deleted through the
    link).
N7. Dump content never reaches `--json` output, history, last-green or any
    public document. Only the reason code and message do.
N8. Ctrl-C / outside cancel latency and behaviour are unchanged: no dump, no
    1 s wait.
N9. `stall_timeout = 0` produces no marker-driven kill. The deadline path still
    dumps.
N10. Literal argv, native stdout/stderr passthrough, exit status mapping for
    non-stall outcomes and coverage gates are unchanged. A healthy run's output
    gains no new lines (non-verbose).
N11. The bridge's dump/marker code must never raise into pytest: any OSError
    is swallowed and the feature degrades to off for that process.
N12. No unbounded growth: dump and marker files are deleted per attempt. A
    process that receives SIGWINCH repeatedly appends at most what
    faulthandler writes per signal, and the printer caps reads anyway.

## Abuse cases (secure-by-spec step 1)

| Axis | Abuse case | Required behaviour / test |
|---|---|---|
| Identity | marker or dump path pre-created as a symlink to a user file | O_EXCL/O_NOFOLLOW refuse; no write through the link; guard does not arm (N6) |
| Identity | dump file replaced by a foreign-owned file before printing | lstat owner/regular check refuses it; nothing printed or deleted through it |
| Authorization | stall fires while the guard no longer anchors its group | `_signal_group` raises ownership-uncertain, as today; nothing outside the group is signalled (N5) |
| Tenancy | two concurrent runs in different worktrees; one arms | the other is unaffected; the path is bound per attempt (N4) |
| Tenancy | multi-attempt run (advanced, a001→a002): a001's marker exists when a002 starts | a002 uses its own derived marker; a stale a001 marker cannot arm a002 |
| Input | test or function names containing ANSI/OSC escape sequences appear in dumps | printed escaped (B6) |
| Input | a huge dump (thousands of threads, deep recursion) | capped per file and in total, with a truncation note (B6) |
| Input | `stall_timeout = -1`, `5`, `"120"`, `1e9`, `true` | config error, exit 2 |
| State | stall and compound deadline fire in the same poll | first problem wins deterministically; one dump; one kill |
| State | user Ctrl-C during the 1 s dump wait | cancel wins promptly; exit 130 semantics unchanged |
| State | tests finished, the group exits normally 1 s later | no stall; normal verdict; marker and dumps cleaned (B7) |
| State | the child exits but a detached grandchild keeps the group alive and idle | existing quiescence/ownership logic decides; stall applies only while the direct child is alive (B1) |
| State | xdist worker crashes (`pytest_testnodedown`) leaving items without outcomes | arms only through (b) when the loop returns; otherwise the deadline governs |
| Exposure | dumps include absolute paths and source lines of the user's code | stderr only, same user; never in JSON, history or doctor model payloads (N7) |

## Test strategy (tests-first, through ptest)

- Unit tests for the arm-condition logic in the bridge (all outcomes / loop
  returned / setup failure / skip / -x / zero items), the CPU-idle window
  (injected samples and clock; unreadable member counts as busy), config
  parsing bounds, problem-code mapping to exit 70 and the end line, the dump
  printer (bounds, escaping, ordering, owner and symlink refusal), and cleanup.
- Real-subprocess tests (the repo already uses fake clocks such as
  `guard.time` replaced by a monotonic-only fake, plus real process groups):
  1. A fixture project whose session fixture teardown blocks forever on
     `threading.Event().wait()` after all tests pass: ptest ends exit 70
     `post-test-stall` and the stderr dump names the blocking fixture function.
     Use a small `stall_timeout` (10) and keep the test under the repo's
     time budgets (a fake clock or the minimum bound).
  2. The same with xdist `-n 2`, if the repo's tests already exercise
     xdist: the dump includes worker stacks.
  3. A slow-but-progressing test (sleeps longer than stall_timeout inside the
     test call): not killed by stall (N1).
  4. Busy post-test work (CPU spin in sessionfinish, shorter than the deadline):
     not killed (N2).
  5. The compound-deadline kill path prints stack dumps (G1).
  6. Ctrl-C path: no dump lines (N8).
- Each abuse test must be observed failing against the pre-change code (or by
  mutating the new control), per secure-by-spec.
- Follow `docs/ptest-agent.md`: only `ptest`, scoped during TDD, one final
  integrated `ptest --full`. Test-quality budgets apply: no wall-clock sleeps
  for synchronisation where a fake clock or event fits.

## Out of scope

- Root-causing the persea hang (the dumps exist to do that next time).
- A CLI flag for stall_timeout; non-pytest stall detection; py-spy.
- Replacing the installed CLI (other jobs are using it).
