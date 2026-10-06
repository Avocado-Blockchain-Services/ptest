# Design: post-test stall detection and stack dumps before kills
Date: 2026-10-06. Author: architect. Branch: feature/post-test-stall (base 3ab75e8).
Authoritative spec: /home/ingmar/worktrees/ptest/specs/2026-10-05-post-test-stall.md
(G1, G2, B1-B10, N1-N12, abuse table). This document only fixes decisions,
interfaces, ownership and tests. Where it is silent, the spec governs.

## 0. Barrier: shared contracts content (applied before T1-T5 start)

All `src/ptest/contracts.py` changes in "Shared-file content" (section 9) are a
barrier. They must be present on the chain base before T1-T5 branch. T3 and T4
construct `LaunchManifest(stall_timeout_s=...)` and `Problem("post-test-stall")`,
and T1/T4 tests compare against the name constants, so none of them can work
without it. T2 is the owner of record for contracts.py. T2 adds no contracts.py
edits beyond the shared content. If the content is already in its worktree, T2
leaves contracts.py untouched and only adds tests.

## 1. Decisions

D1  File naming (frozen, see section 9):
    marker = `str(report_path) + ".done"`;
    dump   = `str(report_path) + f".stack-{pid}"`.
    Both live in the private reports directory
    `<domain>/checkouts/<checkout_id>/reports` (0700). `report_path` is the
    attempt's own `PreparedRun.report_path` (random 32-hex name per attempt),
    so the path is bound per attempt, run and checkout (N4).
D2  The bridge imports no ptest code. It duplicates the three literals as
    module constants `_STALL_MARKER_SUFFIX = ".done"`,
    `_STACK_DUMP_INFIX = ".stack-"` and
    `_STACK_DUMP_HEADER_PREFIX = "ptest stack dump: "`. A T1 test asserts
    they equal the contracts constants.
D3  Dump file header. When the bridge creates its dump file, it writes exactly
    one first line, then registers faulthandler:
      controller: `ptest stack dump: role=controller pid=<pid>\n`
      worker:     `ptest stack dump: role=worker id=<gwN> pid=<pid>\n`
    A file is "non-empty" only if it holds bytes after that first line. This
    is how the printer identifies the controller.
D4  The dump signal (SIGWINCH) is sent only for attempts whose
    `prepared.report_path is not None`. Only pytest has report_path
    (`native_runner == native_pytest`). It is sent only on a deadline kill
    (compound or attempt scope, in the `_run_one` child loop) or a stall kill.
    Vitest/simple/command attempts get no SIGWINCH and no wait, so B9 holds and
    their kill latency is unchanged. Pre-spawn deadline paths (`_ready`,
    `await_attempt_decision`) have no child and send no dump.
D5  Stall detection runs only when all of these hold: phase == "execution",
    `manifest.stall_timeout_s is not None`, `prepared.report_path is not None`,
    and the direct child is alive. Operations passes `stall_timeout_s` only for
    `RunnerKind.PYTEST`. `0` in config maps to `None` in the manifest; an
    absent config key maps to `DEFAULT_STALL_TIMEOUT_S` (120). The manifest
    accepts 0.1..86400 (private protocol, like the other manifest timeouts), so
    tests can use 1 s windows. The config accepts only 0 or 10..86400.
D6  Precedence (guard): in each loop pass, the deadline check comes first,
    then the stall check (skipped once `cancel_signal` is set). First problem
    wins, so there is one dump and one kill. An external cancel (signal handler
    or control "cancel" frame) received before or during the dump wait means
    no dump if it came first, and an immediate end of the wait if it came
    during it.
D7  Verdict (operations `_outcome`): if `guard_problem.code ==
    "post-test-stall"` and there is no user cancellation, the result is
    `(INCOMPLETE, 70, "ptest", None)` whatever the raw code (N3; this is
    stricter than execution-timeout, where a raw 23 still wins). If a user
    cancellation happened, the existing cancel semantics apply (130 etc.).
D8  End line: unchanged format (`ptest: incomplete · … (exit 70)`). The cause
    is the reason line that cli `_emit_reasons` already prints:
    `post-test-stall: tests finished but runner processes stayed idle for 120s
    without exiting; stack dumps above; rerun once alone, report a repeat`.
    If no dump was printed, the suffix is `; rerun once alone, report a
    repeat`. The guard message is frozen (section 2.3). Operations appends the
    suffix. execution-timeout messages are NOT changed, because history keys
    kill_s on their prefix.
D9  Dump printing (operations, after the guard is reaped, before the end
    line) happens only when all hold:
    (a) no user cancellation (`signals.number is None`);
    (b) guard problem code in {post-test-stall, execution-timeout}, OR the
        guard handoff was incomplete (the guard SIGKILLed itself after grace
        and facts were lost);
    (c) at least one non-empty dump exists.
    Output goes to stderr via `print(..., file=sys.stderr)` and is not
    suppressed by `-q` or `--json`. It never goes into RunResult, JSON, history
    or exports (N7).
D10 Cleanup (B7): operations deletes the marker and every dump of each
    attempt's report_path, in a `finally` that runs on every outcome, after
    the guard returned. It deletes only lstat-regular files owned by the user,
    through `files.unlink_if_same` (dev/ino). Symlinks and foreign or
    non-regular entries are left alone (refused, never followed). There is no
    existing reports-dir retention sweep, so none is extended. Leftovers after
    a ptest crash are bounded (1 marker plus at most workers+1 dumps per
    crashed attempt). This is a documented limitation.
D11 Process-global faulthandler registration happens only in a
    ptest-launched bridge process: the controller when the module runs as
    `__main__` (script mode), and xdist workers when it is imported as
    `pytest_bridge` (`-p pytest_bridge`). In-process callers
    (`ptest.runtime.pytest_bridge.run()` from this repo's own tests) never
    register or unregister SIGWINCH, so they cannot steal the outer run's
    registration. Marker arming happens whenever a report binding exists.
D12 No guard protocol change. runner-facts / draining ordering and keys are
    unchanged, and `GUARD_PROTOCOL_VERSION` stays 2. Known limitation: if the
    group survives SIGTERM for the full grace, the guard SIGKILLs itself
    (existing behaviour) and the run ends 70 with protocol-mismatch "guard
    handoff was incomplete". Dumps are still printed per D9(b).
D13 History: no history.py change. `_compound_killed` keys on execution-timeout
    and "compound execution deadline expired", so a stall never feeds kill_s
    (T4 adds a regression test). A shadow-mode stall may add "shadow history
    could not be committed", exactly as an a001 deadline kill does today. The
    run is still incomplete/70.

## 2. Frozen interfaces

### 2.1 contracts.py (shared content, section 9)
- `DEFAULT_STALL_TIMEOUT_S = 120.0`, `MIN_STALL_TIMEOUT_S = 10.0`,
  `MAX_STALL_TIMEOUT_S = 86400.0`, `STALL_DUMP_WAIT_S = 1.0`,
  `STALL_MARKER_SUFFIX = ".done"`, `STACK_DUMP_INFIX = ".stack-"`,
  `STACK_DUMP_HEADER_PREFIX = "ptest stack dump: "`.
- `"post-test-stall" in REASON_CODES`.
- `stall_marker_path(report_path: Path) -> Path`
- `stack_dump_path(report_path: Path, pid: int) -> Path`
- `stack_dump_pid(report_name: str, name: str) -> int | None`
- `RunnerConfig.stall_timeout_s: float | None = field(default=None, repr=False)`:
  None means absent (effective 120), 0 means disabled, otherwise 10..86400.
- `LaunchManifest.stall_timeout_s: float | None = None`: None means no stall
  detection, otherwise 0.1..86400. It is always encoded as key
  `"stall_timeout_s"` and decoded optionally.

### 2.2 Bridge file contract (T1 produces; T3 and T4 consume)
- Marker: created once, by the controller only, with
  `os.open(path, O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC, 0o600)`, empty,
  then closed. If it already exists or any error occurs, it is not created and
  nothing is raised.
- Dump: `os.open(path, O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_APPEND|O_CLOEXEC,
  0o600)`, header line per D3, then
  `faulthandler.register(signal.SIGWINCH, file=<fd int>, all_threads=True,
  chain=True)`. The fd is kept in a module global for the process lifetime.
  Any Exception means skip silently (N11).
- Bridge module constants per D2.

### 2.3 Guard (T3 produces; T4 consumes)
- Problem: `Problem(code="post-test-stall", message=f"tests finished but
  runner processes stayed idle for {stall_s:g}s without exiting",
  phase="guard", retryable=False)`, where `stall_s =
  manifest.stall_timeout_s`.
- `run_guard` treats post-test-stall like execution-timeout at its final
  return (it does not return `_EXIT_PROTOCOL`).
- Module globals `guard._STALL_POLL_S = 1.0` (marker stat and CPU sample
  cadence) and `guard._DUMP_WAIT_S = C.STALL_DUMP_WAIT_S` are read at call
  time, never bound as default arguments. T4's e2e test overrides them by
  prefixing `operations._GUARD_SCRIPT` with
  `import ptest.guard as g; g._STALL_POLL_S = 0.2; g._DUMP_WAIT_S = 0.3; `.
- SIGWINCH goes only through `_signal_group(identity, signal.SIGWINCH)`. The
  guard installs no SIGWINCH handler (it receives its own killpg; the default
  is ignore).

### 2.4 Operations (T4 internal, but other tasks and existing tests rely on it)
- `_launch_guard(domain, grant, prepared, setup=None)` keeps its exact
  signature. Existing spies in test_operations / test_run_deadline /
  test_pytest_scoped_subprocess depend on it. The stall value reaches the
  manifest through a new ContextVar `_STALL_TIMEOUT_S` (default None), set and
  reset around `_run_guard` exactly like `_COMPOUND_TIMEOUT_S`, in both the
  standard path and `_execute_shadow`.
- `_stall_timeout_s(config: C.Config) -> float | None` is a module-level seam:
  None for non-PYTEST; `DEFAULT_STALL_TIMEOUT_S` when the config value is
  None; None when it is 0; otherwise the value.

### 2.5 Config (T2)
- `.ptest.toml [runner] stall_timeout`: int/float, 0 or 10..86400, otherwise
  `invalid-config` via the existing `_fail()` (exit 2). bool, string,
  non-finite, negative, 1..9.99 and >86400 are rejected. It is rendered as
  `stall_timeout = {value:g}` on the line after `full_timeout`, only when set
  (0 renders as `stall_timeout = 0`). There is no CLI flag.

### 2.6 User-visible text (T4 emits it; T5 documents it verbatim)
- Header: `ptest: stack dumps (<n> processes) — <reason>`. <reason> is
  `post-test-stall`, `execution-timeout`, or `guard handoff incomplete`.
- Reason line: per D8.
- Truncation notes start with `ptest: stack dump truncated` (per-file) or
  `ptest: stack dumps truncated` (total/file-count cap).

## 3. Data flow
1. Operations (T4) builds the manifest with `stall_timeout_s` (pytest only)
   and launches the guard.
2. The bridge controller (T1) registers its dump file, then runs pytest.main.
   Each xdist worker registers its dump file at `_worker_bootstrap`.
3. The bridge controller creates the marker when (a) every expected item has
   a final outcome, or (b) `pytest_runtestloop` returns or raises.
4. The guard (T3) loop stats the marker at most every `_STALL_POLL_S`. Once
   it is armed, the guard samples group CPU (platform helper) into
   `stall.IdleWindow`. On stall: `state.fail(post-test-stall, dump=True)`.
   The loop sees `cancel_signal`, `_dump_and_wait` does SIGWINCH plus a wait
   of at most `_DUMP_WAIT_S` that cancel can interrupt, then the existing
   `_cancel_and_reap`. The deadline path does the same with execution-timeout.
5. The guard emits runner-facts with the problem and drains. Operations maps
   the result to 70, prints dumps (`stack_dumps`), and cleans up in a finally.

## 4. Tasks, acceptance criteria and tests

Global rules for every task: secure-by-spec. Write tests first, and show each
abuse test failing against the pre-change code (or a mutation of the new
control) in the task report. Run only `ptest` / `ptest <path>` from the
worktree root, never `ptest --full`. Run `graphify update .` after source
changes. Commit only explicit paths in your own worktree, never .pipeline/.
No real-time sleeps for synchronisation: use sockets, FIFOs, files or
psutil-polled conditions with a watchdog. Integration tests whose wall time
exceeds 3 s carry a one-line comment naming the integration boundary (real
guard plus pytest startup).

### T1 Bridge: arm marker and SIGWINCH dump registration
Owns: src/ptest/runtime/pytest_bridge.py, tests/ng/test_stall_bridge.py (new)
Design:
- Add a small bridge-internal helper (for example `_StallArm`) attached to the
  controller plugin only. `run()` attaches it when `binding is not None`.
  Worker plugins never get one.
  - `expect(nodeids)`: a Counter of raw `item.nodeid` strings.
    - Serial (workers == 1): set from `session.items` after
      pytest_collection_finish. Hook BOTH OwnedPlugin and the AdvancedPlugin
      override.
    - xdist controller: set from the first `pytest_xdist_node_collection_finished`
      ids, in BOTH implementations.
    - An empty expectation never arms through (a).
  - `observe(report)`: a final outcome is `when == "call"` with
    `outcome in {"passed","failed","skipped"}` (so rerunfailures "rerun" is
    not final), or `when == "setup"` with failed or skipped. Call it from
    BOTH pytest_runtest_logreport implementations. Arm when every expected id
    has count(final) >= count(expected).
  - `loop_returned()`: wrap the `(yield)` of BOTH pytest_runtestloop
    implementations in try/finally and arm in the finally. This covers -x,
    maxfail, Interrupted and collection errors.
  - Arm means create the marker per 2.2, at most once per process. Never
    raise; swallow Exception.
- Dump registration per 2.2/D3/D11:
  - Controller: in run(), after all pre-flight validation and immediately
    before `pytest.main`, only if `binding is not None and __name__ ==
    "__main__"`.
  - Worker: in `_worker_bootstrap` after the identity checks pass and before
    returning the plugin, only if `__name__ == "pytest_bridge"`. The worker
    re-validates `PTEST_PYTEST_REPORT_PATH` cheaply (absolute; basename
    matches `_REPORT_NAME`; parent lstat is a dir, owned by uid, mode & 0o077
    == 0, realpath unchanged). On any failure it skips silently.
- No new argv, ini, `-p`, env or stdout/stderr output. pytest's faulthandler
  plugin and `-p no:faulthandler` are untouched.
Acceptance:
- [ ] Serial: marker appears after the last test's call report while a
  session-fixture teardown is still blocked. Uses a direct bridge subprocess
  (harness modelled on test_pytest_parallel_subprocess `_run_bridge`, same
  process group, FIFO/file release, no sleeps).
- [ ] N1: no marker while the last test is still inside its call (blocked on a
  FIFO). Setup-failure and skip items count as final. `-x` after a failure
  arms via (b). Zero items with a collection error arms only via (b). No
  marker exists before any test runs.
- [ ] xdist `-n 2` (real xdist, as the existing parallel tests do): the
  controller arms after all forwarded call reports. Sending SIGWINCH to the
  controller pid and to each worker pid (psutil children) appends all-thread
  stacks to each process's `.stack-<pid>` file, and the worker dump names the
  blocking fixture function.
- [ ] N6/N11: a pre-created symlink at the marker or dump path is not written
  through, the target is unchanged, and the run's exit code and report are
  unchanged. An unwritable reports dir leaves the run unaffected.
- [ ] D11: in-process `run()` leaves `signal.getsignal(SIGWINCH)` and
  faulthandler state unchanged.
- [ ] N10: pytest stdout/stderr of a healthy run are byte-identical in content
  to before (no new lines). The argv passed to pytest.main is unchanged.
- [ ] `_STALL_MARKER_SUFFIX`, `_STACK_DUMP_INFIX`, `_STACK_DUMP_HEADER_PREFIX`
  equal the contracts constants, and the bridge path rule equals
  `C.stall_marker_path` / `C.stack_dump_path`.
- [ ] Existing bridge tests stay green (`ptest tests/ng/test_pytest_bridge_unit.py`,
  the parallel subprocess tests touched by the change set).

### T2 Config and contracts
Owns: src/ptest/config.py, src/ptest/contracts.py (shared content only),
tests/ng/test_config.py, tests/ng/test_contracts.py, docs/schemas/v1/run.json
(expected unchanged: run.json enumerates no reason codes and no runner
timeouts; regenerate only if a schema test proves drift)
Design: `_runner` accepts key `stall_timeout`, plus
`_optional_stall_timeout(table)` per 2.5, passed as
`RunnerConfig(stall_timeout_s=...)`. Render after full_timeout only when set.
Acceptance:
- [ ] Parse: absent gives None; 0, 10, 120, 86400 and 10.5 are accepted.
  -1, 5, 9.99, "120", 1e9, true, nan/inf, 86400.5 give an invalid-config
  error (exit 2 through `ptest` on a fixture config; same shape as invalid
  `timeout`).
- [ ] Render: a config without the key renders byte-identically to before
  (existing EXPECTED_FRESH_TOML stays valid). With the key, the line follows
  full_timeout. Round-trip parse(render(x)) == x.
- [ ] Contracts: REASON_CODES contains post-test-stall. A Problem/Reason with
  that code validates. RunnerConfig bounds and `repr` omit stall_timeout_s.
  LaunchManifest round-trips stall_timeout_s (None, 1.0, 120.0). Decode
  rejects 0, -1, 1e9, "x" and true as protocol-mismatch.
  `stall_marker_path`, `stack_dump_path` and `stack_dump_pid` have positive
  and negative cases (another report's name, "0123", 11 digits, non-ASCII
  digits, empty).
- [ ] Any existing test pinning manifest JSON keys is updated to include
  `stall_timeout_s` (behaviour change owned here).

### T3 Guard: stall detection and dump signal before kills
Owns: src/ptest/guard.py, src/ptest/stall.py (new), src/ptest/platform.py,
tests/ng/test_guard.py, tests/ng/test_stall_window.py (new),
tests/ng/fixtures/processes/stall_workload.py (new),
tests/ng/fixtures/processes/guard_driver.py
Design:
- `stall.py` (pure, no clock, no I/O):
  `idle_threshold_s(window_s) = max(0.5, 0.02 * window_s)`;
  `class IdleWindow(window_s)` with
  `observe(now: float, sample: Mapping[Hashable, float] | None) -> bool`.
  A sample maps member key `(pid, create_time)` to cumulative CPU seconds.
  Progress accumulates per-key positive deltas, and a new key adds its full
  value. Disappearing keys add nothing. `None` (unreadable) resets the window
  (busy, never idle). The method returns True only when samples span at least
  window_s since arming or the last reset and progress over the last window_s
  is below the threshold.
- `platform.group_cpu_times(pgid: int, *, exclude_pid: int,
  limit: int = 8192) -> dict[tuple[int, float], float] | None`: psutil
  process_iter, members with `os.getpgid(pid) == pgid`, non-zombie, pid !=
  exclude_pid. The value is user + system + children_user + children_system.
  The result is None when any member is unreadable or vanishes mid-read
  ambiguously, or when the scan exceeds `limit`.
- guard.py:
  - `_State.dump_requested: bool` and `_State.external_cancel: bool`.
  - `cancel()` (signal handler / control frame / existing internal callers)
    sets external_cancel.
  - `fail(problem, *, dump=False)` records the first problem. It sets
    dump_requested only if this is the first problem and no cancel_signal was
    pending, then closes spawn with SIGTERM without marking external_cancel.
  - In `_run_one`, build the stall watcher per D5. In the child loop: deadline
    check (`fail(..., dump=True)`), then the stall check (at most every
    `_STALL_POLL_S`: lstat marker until armed, where armed means S_ISREG and
    st_uid == getuid; after arming, sample and observe), then:
    `if cancel_signal: if dump_requested and report_path: _dump_and_wait(); _cancel_and_reap(); break`.
  - `_dump_and_wait`: `_signal_group(identity, SIGWINCH)`, then
    `control.poll(min(_POLL_S, remaining))` until `_DUMP_WAIT_S` elapsed or
    external_cancel. It is set up before `grace_deadline`, uses only
    `time.monotonic` (fake-clock safe), and `_sleep` uses select.
  - Final return: post-test-stall is treated like execution-timeout.
- stall_workload.py modes (IPC-ready like guard_workload.py):
  - armed-idle: register faulthandler to a given dump path, create the given
    marker, block.
  - unarmed-idle: block without a marker.
  - armed-busy: marker, CPU spin until released over a socket.
  - armed-idle-child: as armed-idle, plus one child in the same group that
    also registers its own dump file.
- guard_driver.py: optional env `GUARD_STALL_POLL` / `GUARD_DUMP_WAIT` patch
  `guard._STALL_POLL_S` / `guard._DUMP_WAIT_S` (like
  `GUARD_DECISION_TIMEOUT`).
Acceptance (real-process tests use the existing Harness, a manifest with
report_path set and stall_timeout_s = 1.0, and patched poll/wait):
- [ ] Stall: armed-idle is killed, runner-facts carry post-test-stall with the
  frozen message, the guard exits 128 + SIGTERM (143) exactly like
  execution-timeout (`fail()` sets cancel_signal=15; precedent
  tests/ng/test_guard.py deadline-kill asserts of 143), no further
  attempt starts (the later marker is absent), and the dump file holds a
  stack written BEFORE the SIGTERM (assert the dump contains the workload's
  blocking function and the workload's SIGTERM marker came after).
- [ ] armed-idle-child: both processes' dumps are written (group-wide signal).
- [ ] N1: unarmed-idle survives well past window plus a margin, then is
  released, and the attempt ends with raw 0 and no problem.
- [ ] N2: armed-busy survives past the window, is released, and ends normally.
- [ ] N6/N4: a symlinked marker (to a regular file), a FIFO or directory at the
  marker path, or a marker created for a different report name never arms.
- [ ] N9: stall_timeout_s None gives no stall kill, while the deadline kill
  still sends SIGWINCH first (dump written before SIGTERM).
- [ ] D4/B9: an attempt without report_path gets no SIGWINCH on a deadline kill
  and no extra wait (existing timeout tests unchanged).
- [ ] N8: an external cancel (control frame and SIGINT) produces no SIGWINCH
  (dump file has header only / absent) and no added delay. A cancel during a
  patched 5 s dump wait ends the wait promptly (well below 5 s).
- [ ] Same-poll race: deadline and stall both due gives one problem
  (execution-timeout, deadline first), one SIGWINCH, one kill.
- [ ] N5: `_signal_group` ownership failure on SIGWINCH raises
  ownership-uncertain as for SIGTERM (unit test with a forged identity). No
  `os.killpg` call for any other pgid.
- [ ] Unit (test_stall_window.py): IdleWindow threshold boundaries (0.5 s
  floor, 2% fraction), sliding window, reset on None, new and vanished
  members, monotonic fake clock. group_cpu_times excludes exclude_pid, returns
  None when psutil raises for a member (monkeypatched), and returns None past
  the limit.
- [ ] All existing tests/ng/test_guard.py tests stay green with unchanged
  timing assertions.

### T4 Operations: plumbing, dump printing, cleanup, verdict
Owns: src/ptest/operations.py, src/ptest/stack_dumps.py (new),
src/ptest/progress.py, src/ptest/reports.py, tests/ng/test_stack_dumps.py
(new), tests/ng/test_run_deadline.py, tests/ng/test_post_test_stall_e2e.py
(new). progress.py and reports.py are optional touch points: header
formatting may live in progress.py, and report helpers may be reused; neither
is required to change.
Design:
- `stack_dumps.py` (T4 internal API, suggested):
  `collect(report_path) -> tuple[Dump, ...]`,
  `emit(report_path, reason) -> int` (number printed),
  `cleanup(report_path) -> None`.
  - List names with `os.scandir` of the report dir. Examine at most 4096
    entries. Select dumps by `C.stack_dump_pid(report_name, entry.name)`.
  - Read via `files.validate_private_file` and `files.read_regular` (owner,
    regular, 0600, nlink 1, no symlink; anything else is refused and neither
    read nor deleted).
  - Caps: 32 files; per file 400 lines and 32 KiB; 128 KiB total; truncation
    notes per 2.6.
  - Escape every line with `render.terminal_text`.
  - Order: controller (header role=controller) first, then ascending pid.
    Skip header-only files.
  - cleanup deletes the marker and all dumps per D10.
- operations.py:
  - `_stall_timeout_s` seam and the `_STALL_TIMEOUT_S` ContextVar
    (`_launch_guard` passes it into LaunchManifest).
  - `_outcome` gets the post-test-stall branch per D7 at the top.
  - Reason message suffix per D8.
  - Print dumps per D9 right after guard facts are decoded (standard path and
    `_execute_shadow`, per attempt in order a001, a002).
  - cleanup in finally blocks per D10 (standard path: the outer finally when
    `report_binding` is set; shadow: alongside `reports.cleanup_report`).
  - The basic-path compound-deadline history publish stays execution-timeout
    only.
Acceptance:
- [ ] Unit (test_stack_dumps.py): ordering; header-only skipped; caps and
  truncation notes; ANSI/OSC/CSI bytes and C1 controls in function or file
  names come out escaped; symlinked, foreign-owned (simulated via
  monkeypatched getuid/lstat), hard-linked or FIFO dump is refused (not read,
  not deleted, target intact); dumps of another report name are ignored (N4);
  cleanup on every outcome removes only own files.
- [ ] Unit (test_run_deadline.py): `_stall_timeout_s` mapping (non-pytest
  None, absent 120, 0 None, 30 → 30). The manifest built by `_launch_guard`
  (spy on encode) carries stall_timeout_s for pytest and None otherwise.
  `_outcome` for post-test-stall with raw None/0/1/23/-15 gives 70 INCOMPLETE
  "ptest", and with cancellation gives the cancel mapping.
  `history._compound_killed` is False for a post-test-stall summary.
  Existing deadline tests stay green.
- [ ] Guard-fault style test (operations `_GUARD_SCRIPT` override emitting
  post-test-stall facts with raw 0 and 1): status incomplete, exit 70, reason
  line text per D8, `full_gate_eligible` False, no last-green or verified
  record, no stack-dump content in `serialize_run_result` or the
  `--result-json` export (N7).
- [ ] E2E (test_post_test_stall_e2e.py; in-process `operations.execute` like
  test_pytest_scoped_subprocess; `_stall_timeout_s` monkeypatched to 1.0;
  `_GUARD_SCRIPT` prefixed per 2.3):
  (1) serial session-fixture teardown blocked on `threading.Event().wait()`
      after all tests pass gives exit 70, post-test-stall, a stderr header,
      and a dump naming the fixture function; marker and dumps are removed
      afterwards.
  (2) the same with xdist `-n 2` and a 2-slot domain: worker stacks appear.
  (3) compound deadline (monkeypatched `resolve_compound_timeout` → (3.0,
      "cli")) with a blocking test call prints dumps with reason
      execution-timeout.
  (4) Ctrl-C (raise SIGINT in-process once the test started, as
      test_real_cancellation_reaps_guard_and_releases_checkout does): no
      "ptest: stack dumps" on stderr, cancel semantics unchanged.
  (5) a healthy run: no new stderr lines, and the marker and dumps are
      cleaned.
  Integration barrier: this module needs T1 and T3. It carries a module-level
  `pytestmark = pytest.mark.skipif(not (hasattr(guard, "_STALL_POLL_S") and
  hasattr(pytest_bridge, "_STALL_MARKER_SUFFIX")), reason="post-test-stall
  e2e: awaiting T1+T3 integration")`. The orchestrator MUST confirm after
  merging T1-T5 that this module reports its tests as passed, with 0 skipped.

### T5 Docs
Owns: docs/specs/2026-10-05-post-test-stall.md (new, verbatim copy of the
spec), docs/ptest-agent.md, src/ptest/resources/repository-agent-guide.md,
src/ptest/resources/agent-guide.md, README.md, docs/changelog.md,
src/ptest/agent_rules.py, tests/ng/fixtures/previous-guides/3ab75e8-ptest-agent.md
(new; byte copy of the base guide), tests/ng/test_agent_rules.py,
tests/ng/test_resources.py
Acceptance:
- [ ] A new output-table row for `post-test-stall: …` in docs/ptest-agent.md,
  byte-identical in repository-agent-guide.md. Meaning: tests finished,
  runner processes hung in teardown/shutdown, stack dumps printed above,
  exit 70. Action: rerun once alone; if it repeats, report it with the stack
  dump; never edit tests to dodge it; a lasting change is
  `[runner] stall_timeout` in `.ptest.toml` (seconds, default 120, 0
  disables). The guide stays at most 100 lines (reflow prose; never relax the
  limit test), never contains "xdist", "fingerprint" or "expected:", and keeps
  every existing asserted phrase.
- [ ] agent-guide.md gains one short paragraph with the same meaning and
  action. Existing assertions hold.
- [ ] README: a "Reading the output" row for `post-test-stall` and a commented
  `stall_timeout` line in the `[runner]` example of the monorepo child config
  (`# stall_timeout = 120  # pytest: end a run whose tests finished but whose
  processes stay idle; 0 disables`).
- [ ] changelog: a new top section `## Unreleased` describing G1, G2, the new
  key and code. No version bump.
- [ ] agent_rules `_PREVIOUS_GUIDE_SHA256S` gains
  `874642905132b63140bff23f991399f5f008404463696a4680eb64bacf16af4e` with the
  comment `# 04d968c (0.4.8-0.4.9): guide before the post-test-stall row.`
  test_every_shipped_guide_version_hashes_into_previous_set stays green. A new
  test using the 3ab75e8 fixture proves a repo holding the 0.4.9 guide
  upgrades in place (`_guide_kind` → "previous", and rules apply rewrites it
  to the current bytes), while an edited copy still raises already-exists.

## 5. Ownership matrix (pairwise disjoint)
T1: src/ptest/runtime/pytest_bridge.py, tests/ng/test_stall_bridge.py
T2: src/ptest/config.py, src/ptest/contracts.py, tests/ng/test_config.py,
    tests/ng/test_contracts.py, docs/schemas/v1/run.json
T3: src/ptest/guard.py, src/ptest/stall.py, src/ptest/platform.py,
    tests/ng/test_guard.py, tests/ng/test_stall_window.py,
    tests/ng/fixtures/processes/stall_workload.py,
    tests/ng/fixtures/processes/guard_driver.py
T4: src/ptest/operations.py, src/ptest/stack_dumps.py, src/ptest/progress.py,
    src/ptest/reports.py, tests/ng/test_stack_dumps.py,
    tests/ng/test_run_deadline.py, tests/ng/test_post_test_stall_e2e.py
T5: docs/specs/2026-10-05-post-test-stall.md, docs/ptest-agent.md,
    src/ptest/resources/repository-agent-guide.md,
    src/ptest/resources/agent-guide.md, README.md, docs/changelog.md,
    src/ptest/agent_rules.py,
    tests/ng/fixtures/previous-guides/3ab75e8-ptest-agent.md,
    tests/ng/test_agent_rules.py, tests/ng/test_resources.py

## 6. Abuse case → test owner
| Abuse case | Owner / test |
|---|---|
| symlink pre-created at marker/dump path | T1 (bridge refuses), T3 (no arm), T4 (no read/delete) |
| foreign-owned dump replaced before print | T4 test_stack_dumps |
| guard no longer anchors group | T3 unit (_signal_group SIGWINCH) |
| two worktrees / other attempt's marker | T3 (other-name marker never arms), T4 (other-name dumps ignored) |
| a001 marker vs a002 | T3 (per-prepared marker path; multi-attempt manifest) |
| ANSI/OSC in names | T4 test_stack_dumps |
| huge dump | T4 caps |
| stall_timeout -1, 5, "120", 1e9, true | T2 |
| stall + deadline same poll | T3 |
| Ctrl-C during dump wait | T3 (guard), T4 e2e (4) |
| group exits 1 s after arming | T4 e2e (5) / T3 window not elapsed |
| detached grandchild after child exit | T3: detection only while the direct child is alive (existing quiescence tests unchanged) |
| xdist worker crash | T1: no (a) arm with missing outcomes; (b) arms on loop return |
| dumps contain user paths | T4: never in JSON/history/export (N7) |

## 7. Risks
| Risk | Impact | Mitigation |
|---|---|---|
| Tasks run in parallel; e2e needs T1+T3+T4 | e2e can't pass in T4 alone | skipif readiness gate on frozen names; orchestrator verifies 0 skipped after merge |
| Shared contracts not pre-applied | T3/T4 red | barrier in section 0; identical bytes if a task must apply them |
| Group survives SIGTERM grace → guard self-SIGKILL | reason degrades to protocol-mismatch | D9(b) still prints dumps; documented (D12) |
| Older installed ptest reading history with new code | an older reader rejects that reason row | same class as config-uncommitted's addition; rows are skipped by tolerant readers |
| User code overrides SIGWINCH | no dump from that process | harmless; documented |
| Repo guide 100-line cap | test failure | T5 reflows prose; never relax test |

## 8. Out of scope
Root cause of the persea hang, CLI flag, non-pytest stall detection, py-spy,
version bump/tag/push/release, replacing the installed CLI.

## 9. Shared-file content (exact; src/ptest/contracts.py)
See the `sharedFileContent` field of the architect output. It is reproduced
verbatim below.

```text
FILE: src/ptest/contracts.py — apply these ten anchored edits verbatim (each OLD block occurs exactly once at base 3ab75e8). No other contracts.py change.

EDIT A — constants. OLD:
CANCEL_GRACE_S = 3.0
NEW:
CANCEL_GRACE_S = 3.0
DEFAULT_STALL_TIMEOUT_S = 120.0
MIN_STALL_TIMEOUT_S = 10.0
MAX_STALL_TIMEOUT_S = 86400.0
STALL_DUMP_WAIT_S = 1.0
STALL_MARKER_SUFFIX = ".done"
STACK_DUMP_INFIX = ".stack-"
STACK_DUMP_HEADER_PREFIX = "ptest stack dump: "

EDIT B — reason code. OLD:
    "parallel-workers",
    "config-uncommitted",
})
NEW:
    "parallel-workers",
    "config-uncommitted",
    "post-test-stall",
})

EDIT C — RunnerConfig field. OLD:
    timeout_s: float | None = field(default=None, repr=False)
    full_timeout_s: float | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        kind = _check_enum("runner.kind", self.kind, RunnerKind)
NEW:
    timeout_s: float | None = field(default=None, repr=False)
    full_timeout_s: float | None = field(default=None, repr=False)
    stall_timeout_s: float | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        kind = _check_enum("runner.kind", self.kind, RunnerKind)

EDIT D — RunnerConfig validation. OLD:
        for field_name in ("timeout_s", "full_timeout_s"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _check_float(
                    f"runner.{field_name}", value, lo=1, hi=MAX_COMPOUND_TIMEOUT_S))
NEW:
        for field_name in ("timeout_s", "full_timeout_s"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _check_float(
                    f"runner.{field_name}", value, lo=1, hi=MAX_COMPOUND_TIMEOUT_S))
        if self.stall_timeout_s is not None:
            stall = _check_float("runner.stall_timeout_s", self.stall_timeout_s,
                                 lo=0, hi=MAX_STALL_TIMEOUT_S)
            if 0 < stall < MIN_STALL_TIMEOUT_S:
                raise ValueError("runner.stall_timeout_s must be 0 or 10-86400")
            object.__setattr__(self, "stall_timeout_s", stall)

EDIT E — path helpers, inserted immediately before PreparedRun. OLD:
@dataclass(frozen=True, kw_only=True)
class PreparedRun:
NEW:
def stall_marker_path(report_path: Path) -> Path:
    """The post-test arm marker bound to one attempt's native report path.

    The pytest bridge duplicates this rule (it imports no ptest code); the
    guard and the dump printer derive it from here.
    """
    return Path(str(report_path) + STALL_MARKER_SUFFIX)


def stack_dump_path(report_path: Path, pid: int) -> Path:
    """One process's stack dump file bound to one attempt's report path."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("stack dump pid must be a positive int")
    return Path(f"{report_path}{STACK_DUMP_INFIX}{pid}")


def stack_dump_pid(report_name: str, name: str) -> int | None:
    """The PID in ``name`` when it names a dump file of ``report_name``."""
    prefix = report_name + STACK_DUMP_INFIX
    if not name.startswith(prefix):
        return None
    digits = name[len(prefix):]
    if (not 1 <= len(digits) <= 10 or not digits.isascii()
            or not digits.isdigit() or digits[0] == "0"):
        return None
    return int(digits)


@dataclass(frozen=True, kw_only=True)
class PreparedRun:

EDIT F — LaunchManifest field. OLD:
    compound_timeout_s: float | None = None
    compound_timeout_source: str | None = None

    def __post_init__(self) -> None:
        if (not _is_int(self.protocol)
NEW:
    compound_timeout_s: float | None = None
    compound_timeout_source: str | None = None
    stall_timeout_s: float | None = None

    def __post_init__(self) -> None:
        if (not _is_int(self.protocol)

EDIT G — LaunchManifest validation. OLD:
        if (self.compound_timeout_source is not None
                and self.compound_timeout_source not in COMPOUND_TIMEOUT_SOURCES):
            raise ValueError("manifest.compound_timeout_source must be a known deadline source")
NEW:
        if (self.compound_timeout_source is not None
                and self.compound_timeout_source not in COMPOUND_TIMEOUT_SOURCES):
            raise ValueError("manifest.compound_timeout_source must be a known deadline source")
        if self.stall_timeout_s is not None:
            object.__setattr__(self, "stall_timeout_s", _check_float(
                "manifest.stall_timeout_s", self.stall_timeout_s,
                lo=0.1, hi=MAX_STALL_TIMEOUT_S))

EDIT H — manifest encode. OLD:
        "compound_timeout_source": manifest.compound_timeout_source,
    }
NEW:
        "compound_timeout_source": manifest.compound_timeout_source,
        "stall_timeout_s": manifest.stall_timeout_s,
    }

EDIT I — manifest decode allowed keys. OLD:
                       "attempt_ids", "setup_timeout_s", "attempt_timeout_s",
                       "compound_timeout_s", "compound_timeout_source"):
NEW:
                       "attempt_ids", "setup_timeout_s", "attempt_timeout_s",
                       "compound_timeout_s", "compound_timeout_source",
                       "stall_timeout_s"):

EDIT J — manifest decode constructor. OLD:
            compound_timeout_source=obj.get("compound_timeout_source"),
        )
NEW:
            compound_timeout_source=obj.get("compound_timeout_source"),
            stall_timeout_s=obj.get("stall_timeout_s"),
        )
```
