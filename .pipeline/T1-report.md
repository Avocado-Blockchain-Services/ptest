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

---
# T1 report — 0.5 dynamic selection: source index, parse cache, conftest edge, D3 fields (2026-10-07 run)

- taskWorktree: /home/ingmar/worktrees/ptest/cc-dynamic-selection/ptest-T1
- taskBranch: feature/dynamic-selection-T1
- commit: 9ab4608 ("T1: per-content source index, parse cache, conftest edge, D3 static fields")
- status: DONE (all T1 acceptance criteria met; one orchestrator action required below)
- filesOwned: src/ptest/source_index.py, src/ptest/impact.py, tests/ng/test_source_index.py, tests/ng/test_impact.py
- filesTouched: exactly the four owned files (commit 9ab4608: 4 files, +2017/−2). Nothing else committed.

## What was built

`src/ptest/source_index.py` (new, stdlib + contracts/impact helpers only):
- `index_source(raw)` — scopes (body/skeleton fingerprints, raw load chains), classes (skeleton/body/refs), top-level statements (def/class/import/assign/doc/effect kinds, bound names, refs, fingerprints), per-alias ImportIndex records (nested imports get `statement=-1`, `local=None`). Fingerprints are `sha256(ast.dump(include_attributes=False))`; nested defs/lambdas fold into the outer scope; duplicate qualnames merge covering both; leading docstrings are excluded from body fingerprints (planner ignores `doc` statements); `ast` only, never imported/executed (N11).
- `encode_index`/`decode_index` — magic + BE-uint32 `SOURCE_INDEX_VERSION` + canonical JSON; decode returns None on truncated/garbage/version/foreign/wrong-type input (strict per-field validators, exact key set).
- `iter_python_files` (== impact's walk), `read_source` (lstat, no-follow, regular file, `..`/absolute refused, `MAX_FILE_BYTES` cap).
- `build_project_index(root, config, *, key, cache, conftest_edges=True)` — one `get_many` (all keyed digests; `get_many([])` past the scan cap) and at most one `put_many` (misses only); keyed digests via `selection_file_digest` ("" when keyless, and keyless builds never touch the cache); modules/test/support from impact's rules; resolved `imports` reproducing the 0.4 name graph; scope/class/statement refs resolved per the design 2.3 table (aliases, module.attr walks, from-module-vs-attribute, relative, `__init__` re-exports, star expansion with `_` filtering, builtins dropped, last-binding-wins in source order); `reverse` plus synthetic `conftest.py` → tests-under-dir edges when enabled.
- `missing_seed_importers` — reverse edges for seeds absent from the index (deleted files) via their module names, preserving 0.4 deleted-seed reachability.

`src/ptest/impact.py`: `PLANNING` ContextVar (`ptest_impact_planning`); `Impact` gains the ten D3/T5 fields (all defaulted, `project_index` compare/repr-excluded); `plan()` gains keyword-only `key`/`cache`/`conftest_edges=True` (positional signature unchanged; PLANNING used only when both key and cache are None). Each file is read and parsed once per plan via a single index build; graph verdicts come from the index; a byte-exact 0.4 `_legacy_tail` is used only when the index is incomplete (past `MAX_SCAN_FILES`), keeping exact 0.4 kind/files/reason there. `conftest_edges=False` reproduces 0.4.10 (existing suite green unchanged).

## Verification (all through ptest, from the task worktree root)

- TDD red: new `tests/ng/test_source_index.py` run before the implementation → collection ImportError (module did not exist).
- Green: `ptest tests/ng/test_impact.py tests/ng/test_source_index.py` → `133 passed`, `ptest: passed · 133 tests`.
- Neighbours (no regressions): `test_changed_default + test_command_model + test_monorepo_changed + test_natural_loop` → 94 passed; `test_operations + test_pytest_adapter + test_pytest_scoped_subprocess` → 607 passed, 11 skipped.
- `graphify update .` run (graph.json updated).
- `ptest --full` NOT run (final integrated gate belongs to someone else). No push/merge/deploy; staged explicit paths only; `.pipeline/` untouched by the commit.

## Acceptance checklist (design section 5, T1)

- [x] `index_source` tables per 2.1/2.3 (source_index.py:192; tests: sample/kinds/fingerprint-shape).
- [x] Comment/whitespace/line-shift identical fingerprints; default→skeleton-only / body→body-only; method edit keeps class body; nested fold; duplicate merge; unparseable (syntax/UTF-8/oversize) → `parsed=False`; kind table incl. star `("*",)`.
- [x] N11: malicious source (print/file-write/SystemExit) leaves no side effect, `sys.modules` unchanged (index + build).
- [x] encode/decode round-trip equal; None on truncated/garbage/bad-magic/foreign-version/wrong-type/unknown-kind.
- [x] build: modules == `impact._module_names`; reverse == 0.4 name graph (private old-algorithm helper; documented seed-inclusion delta); conftest edges; full 2.3 resolution incl. re-export target keys and star expansion; one `get_many`, ≤one `put_many`; warm build zero `ast.parse`; `key=None` no cache.
- [x] plan: all pre-existing test_impact tests pass unchanged; one read+parse per file (counted); conftest-only import selects the directory (and nothing with `conftest_edges=False`); 8/8 dynamic_ok-True paths and 7/7 False paths per D3 with the index invariant; capped paths 2/7/8 keep exact 0.4 verdicts with `dynamic_ok` False; `get_many` exactly once on capped paths; PLANNING honored, explicit kwargs win; `Impact(...)` with 0.4 kwargs works.
- [x] Tests use the real modules and a dict-backed fake cache only. (`selection_model.py` is dropped per the design — the contracts barrier is the shared model.)
- [x] No database migration. No version bump/tag/push.

## Integration notes (orchestrator must read)

1. BARRIER NOT APPLIED ON THE CHAIN — REQUIRED ACTION: design section 0/12's six contracts.py edits had NOT landed anywhere when this task ran (chain and task worktrees were clean at f478189; T5 owns contracts.py and makes no further edits, so no task will ever land it). To verify against the true frozen interfaces, this task machine-transcribed section 12 into the TASK worktree only (each OLD block asserted to occur exactly once; policy digest still `43bc98485c25`), and left it UNCOMMITTED (`git status` shows `M src/ptest/contracts.py`, unstaged). Commit 9ab4608 contains only the four owned files. Before merging ANY task branch, apply design section 12 verbatim to the chain base first — otherwise every task branch fails to import.
2. `missing_seed_importers` (source_index.py:842) is T1-private API consumed only by `impact.plan`; T5/T6 should use `Impact.project_index` rather than calling it.
3. Semantic choices where the design is silent (pinned by tests, see code comments): leading docstrings excluded from scope/class body fingerprints; `effect`/`doc` statements carry empty `statement_bound`; star-import `ImportIndex.local` is None; terminal module references resolve to `path(module):last-segment`; over-cap builds call `get_many([])` once and put nothing.
4. `RecursionError` from `ast.parse` on pathological nesting propagates, exactly as in 0.4 (same exposure, not a regression).

## Three-pass self-review (dan-jefferies-agent)

- Pass 1 (re-read bytes): found and fixed an O(n²) digest→file scan, last-binding-wins ordering across import/def/assign kinds (new regression test), a missing list-type check in `decode_index` (caught by its own test), a dead duplicate branch, an unused parameter, and a stale docstring. Owned-vs-touched set is exactly the four owned files; no "pre-existing failure" claims (one neighbour-free run only; all suites green in-session).
- Pass 2 (re-anchor): every T1 design bullet mapped above with file:line; no runtime behaviour beyond the brief (verified: CLI reads only preserved Impact fields; no new output; in-process/recorder paths untouched).
- Pass 3 (smell sweep): no duplicated concept (only AST indexer — confirmed by grep); helpers reused from impact, not forked, except `_edge_names` which replays `_import_edges` from records and is pinned by a differential test; new tests fail against no-op implementations (e.g. conftest test fails on 0.4 verdicts, warm-cache test fails if parsing recurs); security via O_NOFOLLOW/lstat/size-cap/strict-decode; no TODOs/stubs; no new deps.
- Claimed-vs-shipped delta: none. Out-of-scope smells: none (RecursionError parity noted above, not fixed by design).
- Confidence: high — 133/133 focused green, 701 neighbour tests green, every acceptance bullet backed by a named passing test.
- Final status: implemented (index + plan + 40 new tests) / verified (red run, green runs, neighbour runs, all via ptest) / not verified (`ptest --full` — belongs to Verify) / deferred (none) / discovered-but-not-fixed (barrier application — orchestrator action, not a code defect).

---

# T1 fix report — star refs, mutating-assign kind, docstring fingerprints (attempt 2)

- taskWorktree: /home/ingmar/worktrees/ptest/cc-dynamic-selection/ptest-T1
- taskBranch: feature/dynamic-selection-T1 (continuation commit on top of 9ab4608)
- status: DONE (all three findings fixed, red→green proven, neighbours green)
- filesOwned: src/ptest/source_index.py, tests/ng/test_source_index.py
- filesTouched: exactly the two owned files (+120/−12 approx). Nothing else committed.
  (`src/ptest/contracts.py` shows as modified in the worktree from the prior
  run's uncommitted barrier transcription — deliberately left unstaged and
  uncommitted, per the standing integration note.)

## Root causes (all three confirmed as reported)

1. Star imports: `build_project_index` filled only `statement_bound` for `*`
   imports; `statement_refs` stayed empty, so T2 `_propagate` (which matches on
   refs only) could never carry a name through a star re-export.
2. Mutating assigns: `_classify` mapped every Assign/AnnAssign/AugAssign to
   "assign", while `_target_names` returns `()` for Attribute/Subscript, so
   such statements had empty bound and were invisible to NAMES/AM/SK.
3. Docstrings: `_body_dumps` and `add_class` stripped leading docstrings, so
   function/class docstring edits changed no fingerprint despite being
   runtime-observable (`__doc__`, `--help`, doctests).

## What was changed

`src/ptest/source_index.py`:
- Star branch now also adds `selection_name_key(path, name)` (the defining
  module's key) to `targets`/`statement_refs` for every non-`_` name the target
  module binds, alongside the existing `rel:name` bound entries (design 2.3).
- New `_is_simple_target` (Name, Tuple/List/Starred-of-Name only); `_classify`
  returns "effect" for Assign/AugAssign/AnnAssign with any non-simple target
  (spec 5 step 3: binds no module-level name ⇒ ambient change). Plain
  `x = 1`, `x += 1`, tuple unpacking stay "assign".
- `_body_dumps` and `add_class` no longer strip leading docstrings; the frozen
  top-level "doc" statement rule is unchanged.

`tests/ng/test_source_index.py` (+3 regression tests):
- `test_build_star_reexport_chain_refs_propagate`: `pkg/__init__.py` star
  re-export of `pkg/core.py`; asserts bound has `__init__:TIMEOUT`, refs have
  `core:TIMEOUT`/`core:REG`, and `tests/test_x.py` resolves to
  `__init__:TIMEOUT`.
- `test_index_source_mutating_assign_targets_are_effect`: pins kinds for
  `X[k] = v` / `m.attr = v` / `m.attr += v` → "effect", plain assigns stay
  "assign".
- `test_index_source_docstring_edit_changes_fingerprint`: function and class
  docstring edits change body but not skeleton fingerprints.

## Verification (all through ptest, from the worktree root)

- Red (fix stashed, tests kept): the 3 new tests fail against the old
  implementation (`3 failed in 7.96s`), all pre-existing tests pass.
- Green: `ptest tests/ng/test_source_index.py tests/ng/test_impact.py` →
  `136 passed` (`ptest: passed · 136 tests`), i.e. 133 pre-existing + 3 new.
- `ptest --full` NOT run (final integrated gate belongs to someone else).
  No push/merge/deploy; staged explicit paths only; `.pipeline/` uncommitted.

## Three-pass self-review (dan-jefferies-agent)

- Pass 1 (re-read bytes): re-read the full diff; notable catch — the worktree
  carries the prior run's uncommitted `contracts.py` barrier transcription; it
  was left untouched and is excluded from the commit. `_is_docstring` is still
  used (top-level "doc" rule). Owned-vs-touched set is exactly the two owned
  files. No "pre-existing failure" claims (red/green both observed in-session).
- Pass 2 (re-anchor): each finding's required fix implemented 1:1, with the
  named regression test. Raw evidence quoted above; no NOT RUN items.
- Pass 3 (smell sweep): no duplicated helper (only AST target classifier —
  confirmed by grep; `_target_names` serves bound extraction, `_is_simple_target`
  serves classification); no new layer; plain-name `+=`/tuple-assign behaviour
  unchanged (pinned in-test); existing `statement_refs`-empty assertion for
  plain imports still holds (136 green). No stubs/TODOs; no new deps.
- Claimed-vs-shipped delta: none. Out-of-scope smells: none.
- Confidence: high — red→green on the exact new tests, neighbours green.
- Final status: implemented (3 fixes + 3 tests) / verified (red run, green
  runs, all via ptest) / not verified (`ptest --full` — belongs to Verify) /
  deferred (none) / discovered-but-not-fixed (none).

---

# T1 fix report — star _Resolver drop, effect statement refs (attempt 3)

- taskWorktree: /home/ingmar/worktrees/ptest/cc-dynamic-selection/ptest-T1
- taskBranch: feature/dynamic-selection-T1 (continuation commit on top of 36a1215)
- status: DONE (both findings fixed, red→green proven, neighbours green)
- filesOwned: src/ptest/source_index.py, tests/ng/test_source_index.py
- filesTouched: exactly the two owned files. Nothing else committed.
  (`src/ptest/contracts.py` still shows as modified in the worktree from the
  original run's uncommitted barrier transcription — deliberately left
  unstaged and uncommitted, per the standing integration note. `.pipeline/`
  untouched by the commit.)

## Root causes (both confirmed as reported, reproduced pre-fix)

1. `_Resolver.__init__` ran `if imp.local is None: continue` before the
   `imp.name == "*"` branch. `index_source` always emits star ImportIndex
   with `local=None`, so `self._stars` was always empty: `bound_names()`
   never expanded stars and the `resolve()` star fallback never fired.
   Probe A (3-level chain `__init__ -> mid -> core`): `__init__` star refs
   held only `mid:OWN`, TIMEOUT missing (mid's own star was dropped from
   its bindings, so `bound_names(mid)` was `{OWN}`).
   Probe B (`from pkg.consts import *` + `def get(): return LIMIT`):
   `scope_refs['get']` was `[]` — LIMIT unresolved and dropped.
2. The attempt-2 `_classify` change (mutating/mixed assigns → "effect")
   combined with the `else` branch in `build_project_index` (empty
   `statement_refs`/`statement_bound` for every non-import/def/class/
   assign statement) dropped refs `index_source` had computed. Probe C
   (`x = d['k'] = LIMIT`): kind effect, refs `[]`, bound `[]` — a LIMIT
   change reached neither x nor AM, and x left `_bindings` so later
   references to x went unresolved.

## What was changed

`src/ptest/source_index.py` (two hunks):

- `_Resolver.__init__`: the `imp.name == "*"` branch now comes before the
  `imp.local is None` skip, so star imports land in `_stars`. The same loop
  now also binds `"effect"` statements' plain Name targets (`stmt.bound`
  holds only those; pure mutating targets yield `()` so nothing is added
  for them) — mixed assigns like `x = d['k'] = LIMIT` keep `x` resolvable
  elsewhere in the file.
- `build_project_index` statement loop: `"effect"` joins the
  `("def", "class", "assign")` branch — refs resolve through the resolver
  (design 4.4: upstream changes match effect statements and propagate
  through AM) and plain-Name bound keys are kept (pre-fix assign behavior
  for NAMES). The `else` branch is now `"doc"`-only (still empty).
  Note: each star level's refs point at its direct target's keys
  (`__init__` star → `mid:TIMEOUT`, `mid:OWN`); the planner fixpoint
  carries a `core:TIMEOUT` change transitively via mid's bound keys.

`tests/ng/test_source_index.py` (+3 regression tests):

- `test_build_star_chain_three_levels_propagate`: 3-level star chain with
  `OWN = 1` alongside; asserts mid star refs `core:TIMEOUT`, top star refs
  `mid:TIMEOUT` + `mid:OWN`, top bound `__init__:TIMEOUT`, and the test
  resolving to `__init__:TIMEOUT`.
- `test_build_star_imported_name_resolves_in_function_scope`: star-fed
  `LIMIT` resolves inside `get()`'s scope refs to `consts:LIMIT`.
- `test_build_effect_statements_resolve_refs_and_keep_plain_bound`:
  `x = d['k'] = LIMIT` keeps refs `consts:LIMIT` + bound `app:x`;
  `SETTINGS['limit'] = LIMIT` keeps refs `consts:LIMIT` with empty bound;
  `(a, b.c) = 1, 2` keeps bound `app:a`.

## Verification (all through ptest, from the task worktree root)

- Probes pre-fix (`/tmp/t1_probe.py`, kept out of the repo): all three
  failure shapes observed as diagnosed above.
- Red (src fix stashed, tests kept):
  `ptest tests/ng/test_source_index.py -k "three_levels or function_scope
  or resolve_refs_and_keep_plain_bound"` → `3 failed` (exactly the 3 new
  tests), fix restored via `git stash pop`.
- Green: `ptest tests/ng/test_source_index.py tests/ng/test_impact.py` →
  `139 passed` (`ptest: passed · 139 tests`), i.e. 136 pre-existing + 3 new.
- Neighbours: `test_changed_default + test_command_model +
  test_monorepo_changed + test_natural_loop` → 94 passed;
  `test_operations + test_pytest_adapter + test_pytest_scoped_subprocess`
  → 607 passed, 11 skipped.
- `ptest --full` NOT run (final integrated gate belongs to someone else).
  No push/merge/deploy; staged explicit paths only.

## Three-pass self-review (dan-jefferies-agent)

- Pass 1 (re-read bytes): re-read the full src diff; one notice — the
  `_from_rest` star guard (`imp.name == "*"` → empty) is now unreachable
  in practice (stars never enter `_bindings`) but harmless as defense;
  kept. Grepped every `stmt.kind`/`kind ==` dispatch site in
  source_index.py + impact.py: index_source assignment, _Resolver loop,
  and build loop are the only three; all consistent. Owned-vs-touched set
  is exactly the two owned files (contracts.py modification is the prior
  run's barrier transcription, untouched). No "pre-existing failure"
  claims (red/green both observed in-session).
- Pass 2 (re-anchor): finding 1's required fix (stars into `_stars`) plus
  its two named regression tests (3-level chain, star name in non-test
  function) implemented 1:1; finding 2's required fix (effect
  statement_refs per design 4.4, optional plain-Name bindings kept) plus
  mixed/mutating coverage implemented. Raw evidence quoted above.
- Pass 3 (smell sweep): no duplicated concept (single star check, single
  kind tuple per loop — confirmed by grep); no new layer or helper; no
  underscore-filter change (bound_names unfiltered as before, filtering
  stays at the star-expansion call sites); security axis untouched
  (AST-only, no new I/O); doc-statement emptiness pinned by the
  pre-existing kind-table test (139 green). No stubs/TODOs; no new deps.
- Claimed-vs-shipped delta: none. Out-of-scope smells: none.
- Confidence: high — probe-confirmed diagnoses, red→green on the exact
  new tests, full focused + neighbour suites green.
- Final status: implemented (2-hunk fix + 3 tests) / verified (probes,
  red run, green run, neighbour runs, all via ptest) / not verified
  (`ptest --full` — belongs to Verify) / deferred (none) /
  discovered-but-not-fixed (none).
