# T1 report — Bridge: arm marker and SIGWINCH faulthandler dump registration

- taskWorktree: /home/ingmar/worktrees/ptest/cc-post-test-stall/ptest-T1
- taskBranch: feature/post-test-stall-T1
- commit: 900decf ("T1: bridge stall marker arm and SIGWINCH dump registration")
- status: DONE (all acceptance criteria met; one documented integration note below)
- filesOwned: src/ptest/runtime/pytest_bridge.py, tests/ng/test_stall_bridge.py
- filesTouched: exactly the two owned files (2 files, +1055/−2). Nothing else.

## What was built

`src/ptest/runtime/pytest_bridge.py` (stdlib-only additions: faulthandler,
signal, stat, collections.Counter):

- Frozen literals (design D2): `_STALL_MARKER_SUFFIX = ".done"`,
  `_STACK_DUMP_INFIX = ".stack-"`, `_STACK_DUMP_HEADER_PREFIX = "ptest stack dump: "`.
- `_StallArm` (controller-only): `expect()` records a Counter of raw
  collected node ids; `observe()` counts a final outcome per report
  (call-phase outcome in passed/failed/skipped — rerunfailures "rerun" is
  not final — or setup-phase failure/skip; teardown never counts);
  `loop_returned()` arms when the runtest loop returns/raises (-x, maxfail,
  interrupts, collection errors, worker crash). Empty expectation never arms
  through outcomes. Arming creates the marker at most once and never raises.
- Marker: `os.open(path, O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,
  0o600)`, empty, then closed; any failure returns False silently (N11).
- Dump: `os.open(path, O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_APPEND|O_CLOEXEC,
  0o600)`, exactly one header line (`ptest stack dump: role=controller
  pid=<pid>` / `ptest stack dump: role=worker id=<gwN> pid=<pid>`), then
  `faulthandler.register(SIGWINCH, file=fd, all_threads=True, chain=True)`;
  fd kept in `_STACK_DUMP_FDS` for process lifetime; any failure closes and
  returns False silently (N11). Symlinks refused by O_NOFOLLOW (N6).
- Wiring: `run()` attaches the arm when a report binding exists and, only if
  `__name__ == "__main__"`, registers the controller dump immediately before
  `pytest.main`. Both `pytest_collection_finish` implementations set the
  serial expectation (workers == 1); both `pytest_runtestloop`
  implementations wrap `(yield)` in try/finally → `loop_returned()`; both
  `pytest_runtest_logreport` implementations call `observe()`; the base
  `pytest_xdist_node_collection_finished` sets the first-worker's raw
  collection as the xdist expectation. `_worker_bootstrap` registers the
  worker dump only if `__name__ == "pytest_bridge"`, after identity checks,
  via cheap never-raising `_worker_report_path()` re-validation. Worker-half
  plugins never get an arm. No new argv/ini/-p/env/stdout lines; pytest's
  faulthandler plugin and `-p no:faulthandler` untouched.

## Verification (all through ptest, from the worktree root)

- TDD red: new `tests/ng/test_stall_bridge.py` run BEFORE the implementation:
  24 failed (AttributeError on the new bridge names), 3 passed (the
  no-change guards that must pass both before and after). Evidence in
 Ticket transcript: `24 failed, 3 passed in 56.73s`.
- TDD green after implementation: `ptest tests/ng/test_stall_bridge.py` →
  `28 passed in 18.66s`, `ptest: passed · 28 tests`.
- Neighbours (no regressions): `ptest tests/ng/test_pytest_bridge_unit.py
  tests/ng/test_pytest_parallel_subprocess.py
  tests/ng/test_pytest_xdist_serial_subprocess.py` together with the stall
  file → `2 failed, 87 passed`; both failures were test-side bugs fixed in
  the same file (faulthandler-enabled assertion made relative instead of
  absolute; idle-worker dump wrongly required to name the blocked test;
  xdist `load` round-robin needed the blocked test last in file order).
  Final stall-file state is 28/28 green; neighbour files pass (87 passed
  includes them — no neighbour file appears in any FAILED line).
- `ptest` bare: reports "no changes since last green run" (change set green).
  `ptest --full` NOT run (final integrated gate belongs to someone else).
- Abuse tests observed failing pre-change: the symlink-refusal, garbage-
  report, rerun-outcome, empty-expectation and loop-return tests all failed
  with AttributeError before the implementation existed and pass after it.

## Acceptance checklist (design section 4, T1)

- [x] Serial marker after last call report while session-teardown blocked
      (direct bridge subprocess, FIFO-free file gates with watchdogs).
- [x] N1: no marker mid-call; setup failure/skip final; -x via loop return;
      zero-item collection error via loop return only; in-test assertions
      prove no marker before/during runs.
- [x] xdist -n 2: controller + 2 worker dumps on SIGWINCH, headers exact,
      executing worker names the blocked test; marker absent until the last
      outcome, present after (xdist arm path (a) proven, not just loop return).
- [x] Worker crash (SIGKILL) with missing outcomes: no arm in flight, marker
      only after the loop returns.
- [x] N6/N11 unit + subprocess: symlinked marker/dump refused, targets
      intact, exit codes/reports unchanged, missing dirs silent.
- [x] D11: in-process `run()` leaves SIGWINCH handler and faulthandler state
      untouched and passes argv through byte-identical.
- [x] N10: healthy run has no `stack dump` bytes on stdout/stderr, no refusals.
- [x] Constants equal frozen literals; equality with contracts asserted
      conditionally (see integration note).
- [x] No database migration generated. No push/merge/deploy. Staged explicit
      paths only; `.pipeline/` untouched.

## Integration note (not blocked)

The shared contracts barrier (design section 0/9: `STALL_MARKER_SUFFIX`,
`STACK_DUMP_INFIX`, `STALL_DUMP_HEADER_PREFIX`, `stall_marker_path`,
`stack_dump_path` in contracts.py) had NOT landed in any worktree at
implementation time, and contracts.py belongs to T2, so T1 could not use it.
The bridge duplicates the three literals per D2, and
`test_stall_constants_match_contracts` asserts the frozen literal values
plus equality with contracts whenever those names exist (vacuous until the
barrier lands, strict afterwards). No local shim of contracts functions was
needed. T3/T4 consume only the frozen file/header/hook behaviour delivered
here; no other seam.

## Three-pass self-review (dan-jefferies-agent)

- Pass 1 (re-read bytes): found and fixed — an unused `import re` in the new
  test file; a broken hook-semantics refactor attempt (helper returning a
  generator instead of yielding in the wrapper) reverted to inline
  try/finally; two over-strict assertions corrected to truthful ones
  (relative faulthandler-enabled check; idle-worker serve-loop dumps).
  Owned-vs-touched set matches exactly the two owned files.
- Pass 2 (re-anchor): every T1 design bullet mapped to a hook/test above;
  no runtime behaviour beyond the brief (verified: no new output, argv
  identical, in-process registration absent).
- Pass 3 (smell sweep, audit-spec dimensions): no duplication (no prior
  stall/marker/faulthandler concept in the bridge — confirmed against HEAD);
  no new helpers beyond the specified ones; stdlib-only imports; no-deps;
  O_NOFOLLOW/O_EXCL/0600 + lstat checks + gwN validation cover the security
  axis; every test fails against a no-op implementation except the three
  deliberate no-change guards (which pin N6/N10/N11 invariance).
- No "pre-existing failure" claims made. No stubs/TODOs left.
- Confidence: high — 28/28 focused tests green plus neighbour suites green,
  every acceptance bullet backed by a named passing test.

---

# T1 fix report — subtest reports must not arm the stall marker (attempt 2)

- taskWorktree: /home/ingmar/worktrees/ptest/cc-post-test-stall/ptest-T1
- taskBranch: feature/post-test-stall-T1 (continuation commit on top of 900decf)
- status: DONE (finding fixed, red→green proven, neighbours green)
- filesOwned: src/ptest/runtime/pytest_bridge.py, tests/ng/test_stall_bridge.py
- filesTouched: exactly the two owned files (+142/−1). Nothing else.

## Root cause (confirmed as reported)

`_StallArm.observe` counted any call-phase report with outcome
passed/failed/skipped as the item's final outcome. pytest 9.1.1's builtin
subtests emit one `SubtestReport` (a `TestReport` subclass) per
`subtests.test(...)` block with `when="call"`, the parent's nodeid and a
final-looking outcome — before the parent's own call report arrives. The
first subtest of the last test therefore completed the expected count and
`_maybe_arm` created the marker mid-call. I reproduced this against the
unfixed code with a probe driving the real `_StallArm` with a
subtest-shaped report carrying a real `SubtestContext`: `MARKER_EXISTS_MIDCALL=
True`. I also confirmed the fix's discriminators against the installed
pytest 9.1.1 sources: `SubtestReport` declares a `context: SubtestContext`
attribute that plain `TestReport` instances lack (observed `hasattr` False),
and xdist's `pytest_report_from_serializable` reconstructs a genuine
`SubtestReport` (with `context`) on the controller, so the same guard holds
under xdist.

## What was changed

`src/ptest/runtime/pytest_bridge.py` (+32/−1):

- New `_SUBTEST_REPORT_TYPE_NAMES = frozenset({"SubtestReport",
  "SubTestReport"})` next to `_FINAL_CALL_OUTCOMES`.
- New never-raising `_is_subtest_report(report)`: True when any class in
  `type(report).__mro__` is named `SubtestReport` (builtin) or
  `SubTestReport` (third-party pytest-subtests plugin, not installed here —
  defensive), or when the report carries a non-None `context` attribute
  (the robust path: catches genuine builtin reports, subclasses, and
  xdist-deserialized ones). Checked `item.context_expr` at bridge line
  ~1404 is `ast.withitem` API, unrelated — no false-positive source.
- `_StallArm.observe` returns early for subtest reports, before nodeid
  extraction. Both `OwnedPlugin.pytest_runtest_logreport` and
  `AdvancedPlugin.pytest_runtest_logreport` funnel through `observe`, so
  this is the single fix point for serial and xdist. `_StallArm`
  docstring updated accordingly. No new imports, no new behaviour for
  non-subtest reports.

`tests/ng/test_stall_bridge.py` (+111):

- `_genuine_subtest_report` helper builds a real
  `_pytest.subtests.SubtestReport` via `SubtestReport._new` (no mocks).
- `test_subtest_reports_do_not_arm_mid_call`: genuine passed then failed
  subtest reports of the last test leave the marker absent; the parent's
  own call report arms it.
- `test_subtest_plugin_report_shape_never_counts`: the third-party
  `SubTestReport` class-name shape (no `context`) never counts; the real
  call report still arms.
- `test_no_marker_between_subtests_of_last_test`: serial subprocess twin —
  last test runs two `subtests.test(...)` blocks with a file gate held
  open between them; asserts the marker absent in-test after each subtest
  and from the parent during ~1 s of polling, then present after the
  parent finishes. Explicit timeout respected (`communicate(timeout=60)`,
  watchdog polls, no bare sleeps).

## Verification (all through ptest, from the worktree root)

- Red (fix stashed, tests kept): `ptest tests/ng/test_stall_bridge.py` →
  `3 failed, 28 passed in 53.68s`: exactly the 3 new tests fail, all 28
  pre-existing tests pass. Each new test therefore fails against the old
  implementation (no vacuous pass).
- Repro probe post-fix: `MARKER_EXISTS_MIDCALL= False` (probe at
  /tmp/subtest_repro.py, kept out of the repo).
- Green: `ptest tests/ng/test_stall_bridge.py` → `31 passed in 22.82s`,
  `ptest: passed · 31 tests`.
- Neighbours: `ptest tests/ng/test_pytest_bridge_unit.py
  tests/ng/test_pytest_parallel_subprocess.py
  tests/ng/test_pytest_xdist_serial_subprocess.py` → `61 passed`.
- `ptest` bare: "no changes since last green run" (change set green).
  `ptest --full` NOT run (final integrated gate belongs to someone else).

## Acceptance re-check

- Criterion 1 / spec N1 restored: the marker is created only once every
  item has a final outcome; a still-running test using subtests is never
  counted as finished, however idle it is between subtests. The
  previously-true failure path (last test idle after its first subtest →
  T3 kills a running test as post-test-stall, exit 70) is closed: the
  marker cannot arm until the parent's own call report arrives.
- No other acceptance bullet affected (all 28 prior tests unmodified and
  green; N6/N10/N11/D11 behaviour untouched).

## Three-pass self-review (dan-jefferies-agent) + audit-spec sweep

- Pass 1 (re-read bytes): re-read the full diff; one cosmetic notice — the
  `_StallArm` docstring edit leaves the pre-existing "Teardown reports /
  never gate arming" line break as-is (matches file style; kept, not
  worth another test cycle). Owned-vs-touched set is exactly the two
  owned files. No "pre-existing failure" claims (red/green both observed
  in-session on this worktree).
- Pass 2 (re-anchor): the finding's required fix (skip subtest reports;
  unit test + serial subprocess twin checking marker-absence between two
  subtests of the last test) is implemented 1:1. Verified with raw
  evidence: quoted ptest outputs above; no NOT RUN items.
- Pass 3 (audit-spec dimensions): correctness — genuine-class unit test +
  end-to-end twin, xdist path traced through
  `pytest_report_from_serializable`; refutation attempted for
  `context`-attribute false positives (plain TestReport lacks it;
  neighbour suites green with the fix, so no Mock-shaped report is
  wrongly skipped). Architecture — single fix point, no new layer.
  Security — guard never raises, N11 intact. Tests — old-code failure
  proven per test, not just the file. No stubs/TODOs; no new deps.
- Claimed-vs-shipped delta: none — everything in the finding was done.
- Out-of-scope but smells off: none observed.
- Confidence: high — red→green on the exact new tests, genuine (unmocked)
  subtest objects throughout, neighbours green.
- Final status: implemented (fix + 3 tests) / verified (red run, green
  run, neighbour run, all via ptest) / not verified (none) / deferred
  (none) / discovered-but-not-fixed (none).
