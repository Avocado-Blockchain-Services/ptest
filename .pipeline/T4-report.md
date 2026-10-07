# T4 report — Bridge recorder: local sys.monitoring + audit-hook recording and deselect binding

status: DONE
taskWorktree: /home/ingmar/worktrees/ptest/cc-dynamic-selection/ptest-T4
taskBranch: feature/dynamic-selection-T4
commit: 4121152 ("T4: bridge recorder (sys.monitoring + audit hook) and deselect binding")
date: 2026-10-07

## What was built

- `src/ptest/runtime/selection_recorder.py` (new, stdlib-only, no ptest imports):
  first free tool id among (3, 4, 2) via `use_tool_id(i, "ptest-selection")`;
  `PY_START` only through `set_local_events` on project code discovered via
  the audit `exec` event (`co_consts` walked recursively) plus one
  `sys.modules` walk at activation; callback files the code and returns
  `DISABLE`; `restart_events()` plus a tool-ownership check on every context
  switch (tamper → opaque + recording stops). Test / fixture keyed
  `(baseid, argname, scope)` / ambient contexts; worst-phase outcomes
  (error > failed > xpassed > passed > xfailed > skipped, else unknown);
  per-context caps → opaque; encoded-size cap → overflow file; one
  `<report>.deps-<pid>` per process (O_EXCL|O_NOFOLLOW|O_CLOEXEC, 0600).
  Exact inactive reasons: `Python 3.N has no sys.monitoring`,
  `no free sys.monitoring tool id`, `recorder could not start`.
- `src/ptest/runtime/pytest_bridge.py`: controller activation before the
  `sys.path` swap (only `__main__` + `PTEST_SELECTION_RECORD=1` + binding);
  worker activation at `-p` import; new `pytest_fixture_setup` wrapper
  (tryfirst) + module delegate + `_BRIDGE_HOOK_MARKS` entry; protocol /
  logreport wiring; scoped deselect binding validation with
  `pytest_deselected`; session-end deps writes (serial/xdist controller in
  `run()`, worker-half `pytest_sessionfinish`). Any recorder failure
  degrades to unrecorded, never to a failed run.
- Tests: `tests/ng/test_selection_recorder.py` (28 tests),
  `tests/ng/test_selection_bridge_subprocess.py` (19 tests), fixture
  project `tests/ng/fixtures/selection_project/` (6 files, `.txt` suffix
  so the suite never collects them).
- `test_pytest_bridge_unit.py` untouched: no pinned hook-table expectation
  exists, so the allowed conditional edit was not needed.

## Verification (all `ptest` from the worktree root)

- `ptest tests/ng/test_selection_recorder.py tests/ng/test_selection_bridge_subprocess.py`
  → 41 passed, 6 skipped (working dir: worktree root, exit 0). The 6 skips
  are the documented integration gates: 3 pin-to-contracts tests await the
  T5 contracts barrier, 3 cross-checks await T3 `selection_ingest`.
- Regressions: `test_pytest_bridge_unit.py` + `test_stall_bridge.py` →
  61 passed; `test_pytest_scoped_subprocess.py` +
  `test_pytest_parallel_subprocess.py` → 101 passed, 11 skipped
  (pre-existing skips, no failures).
- TDD: recorder tests failed at collection pre-implementation
  (`ImportError: selection_recorder`); each bridge-twin failure below was
  observed failing first, then fixed.

## Failures found by tests (all fixed, all now green)

1. Controller activated after `run()` swapped `sys.path[0]` to the
   checkout, so the sibling import failed and every file came out
   `recording:false`. Fix: activate before the swap, attach to the plugin
   after validation (`_activate_controller_selection`).
2. Code objects compare structurally: an empty `pkg/__init__.py` collided
   with an already-cached stdlib stub and its module record vanished. Fix:
   identity-keyed cache (`id()` + anchoring ref), project entries only.
3. pytest escapes non-ASCII/newline node ids (`\xfc…`, `\n`); the binding
   and expectations now use the escaped forms (consistent with what the
   recorder stores and T3/T5 will pass back).
4. Fixture refs decoded as tuples; shadow twin also copies
   `protocol-v1.json` (descriptor refusal is unrelated to recording).

## Pass-1 surface (re-read of own bytes)

- The structural code-equality collision above is the thing writing alone
  did not reveal; the exact-content JSON test caught it.
- `Recorder._payload` drops node fixture refs whose setup never ran in
  this process (e.g. fixtures a skipped test never instantiated); a
  referenced-but-unset key therefore means "never contributed", which is
  the correct record. No code change.

## Claimed-vs-shipped delta / deferred

- Crashed-worker-leaves-no-file is structural (writes happen only at
  session end) and is not covered by a dedicated kill test.
- `_DATA_MAX_BYTES` guards D(T): data files above the cap are not tracked.
- No migration generated. No push, no merge, no deploy, no live-database
  contact. Staged explicit paths only; `.pipeline/` untouched.

## Integration notes (seams for T3/T5, not blockers)

- BARRIER NOT PRESENT: this worktree branched at f478189 and the chain
  worktree has no contracts-barrier changes, so `contracts.py` has no
  `SELECTION_*` yet and was deliberately NOT touched (T5 owns it).
  Per the task brief the missing shape is declared locally: the recorder
  and bridge duplicate every literal/predicate from design §12 verbatim.
  Gated tests (`_NEEDS_BARRIER`, `_NEEDS_INGEST`) skip until the barrier
  and T3 land; the orchestrator must confirm they run (not skip) post-merge.
- Fixture baseid observed: pytest reports a session fixture's
  `FixtureDef.baseid` as the conftest directory (`tests`), not `""`.
  Recorder, bridge and tests all use the def's own attributes, so keys
  agree by construction; T2/T3 consume the same `(baseid, argname, scope)`
  triple.
- `pytest_fixture_setup` was added to `_BRIDGE_HOOK_MARKS` as
  `{"wrapper": True, "tryfirst": True}`; no existing test pins that table.

## Confidence: high

Every acceptance bullet is exercised by a test that was seen failing
first, except the two deferred items above; neighboring suites
(61 + 101 tests) show no behavior change.

---

# T4 fix report — three review findings (2026-10-07)

taskWorktree: /home/ingmar/worktrees/ptest/cc-dynamic-selection/ptest-T4
taskBranch: feature/dynamic-selection-T4
priorCommit: 4121152

## Fixes (all in `src/ptest/runtime/selection_recorder.py`)

1. [BLOCKER] Node fixture refs are now indexes into the top-level
   fixtures array (`_payload`: `ordered_fixtures` + `fixture_index`;
   frozen §2.6). Tests updated to the contract shape:
   `test_selection_bridge_subprocess.py::_decode` resolves int refs via
   the fixtures array, and `test_xdist_recording_merges_workers` asserts
   each ref is an int in range and that a node refs the `booted`/session
   key. `test_session_fixture_call_belongs_to_fixture_key` keeps passing
   through the fixed `_decode`.
2. [HIGH] `_resolve_data` no longer drops data files above
   `_DATA_MAX_BYTES`; the path is recorded whatever its size (spec N1).
   The constant is kept as a frozen literal pinned to contracts
   (`SELECTION_DATA_MAX_BYTES` is a design §12 literal; both pin tests
   still hold) with a corrected comment: it documents the engine-side
   digest bound and is never a recorder drop filter.
3. [HIGH] `_walk_modules` no longer calls `loader.get_code` (which armed
   an unmarshalled copy and could write `.pyc`). It now filters modules
   on `__file__` inside the checkout via `_module_is_project` and arms
   live code via `_arm_live` (functions' `__code__`, class members,
   staticmethod/classmethod/property accessors, nested project modules
   only; `vars()` so no descriptors fire). Non-project code is still
   filtered by `_discover` itself.

## New regression tests (`tests/ng/test_selection_recorder.py`)

- `test_node_fixture_refs_are_fixtures_indexes` (two fixtures, refs
  `[0, 1]` plus the T3 `int`-in-range predicate).
- `test_oversize_data_file_is_still_recorded` (sparse 20 MiB
  `data/big.bin` resolves to `"data/big.bin"`).
- `test_activation_arms_modules_imported_before_it` (child interpreter:
  import before `activate()`, `get_local_events != 0` on the live
  function and method codes, and both calls recorded in the test
  context).

## Verification (all `ptest` from the T4 worktree root)

- New tests fail on the unfixed recorder (stash proof): 3 failed,
  25 passed — the 3 failures are exactly the new tests.
- `ptest tests/ng/test_selection_recorder.py` → 28 passed, 3 skipped
  (exit 0). The 3 skips are the pre-existing barrier/ingest gates.
- `ptest tests/ng/test_selection_bridge_subprocess.py` → 16 passed,
  3 skipped (exit 0).
- Neighbor: `ptest tests/ng/test_pytest_bridge_unit.py` → 26 tests
  passed (exit 0).
- Independent probe (`/tmp/t4t3_ingest_probe.py`, not committed): a
  real deps file with a session-fixture ref was read by T3's actual
  `selection_ingest.read_run` (ptest-T3 worktree) → `complete: True`,
  wire refs `[0]`.

## Notes

- No test was weakened, skipped, or deleted; the two `_DATA_MAX_BYTES`
  pin tests pass unchanged.
- Pass-1 observation: `_module_is_project` inherits `_resolve_code`'s
  `realpath == abspath` strictness, so a symlinked checkout root arms
  nothing — pre-existing, consistent with `_discover`, left as is.
- No push, no merge, no deploy, no live-database contact. Staged
  explicit paths only in the T4 worktree; this `.pipeline/` append is
  left uncommitted in the chain worktree.

---

# T4 fix report — prior finding 3, second pass (2026-10-07)

taskWorktree: /home/ingmar/worktrees/ptest/cc-dynamic-selection/ptest-T4
taskBranch: feature/dynamic-selection-T4
priorCommit: df447e8

## Gap

`_arm_live` armed only undecorated functions and class members, so
project code imported before `activate()` recorded differently from
the same module imported after it (exec-audit `co_consts` walk):
`functools.lru_cache` originals (non-FunctionType wrappers),
`functools.wraps` originals (closure cells / `__wrapped__`),
`@contextlib.contextmanager` generators (`__wrapped__` on a stdlib
helper), and lambdas in dicts/lists were never armed.

## Fix (all in `src/ptest/runtime/selection_recorder.py`, `_arm_live`)

- FunctionType: after `_discover(code)`, also follow `__wrapped__`
  and every `__closure__` cell's `cell_contents`.
- New `types.MethodType` branch: follow `__func__`.
- Non-function members carrying `__wrapped__` (lru_cache wrappers):
  read from the instance `vars()` dict first so no descriptor fires;
  `getattr` fallback only when `vars()` raises `TypeError`.
- `dict` values and `list`/`tuple` items are walked, so container-held
  callables are armed.
- The member loop now pushes every non-module member (modules stay
  project-gated); modules arriving via containers are re-checked with
  `_module_is_project`. Non-project code is still filtered by
  `_discover` itself. No signature changes, stdlib only, no new deps.

## New regression test (`tests/ng/test_selection_recorder.py`)

- `test_activation_arms_decorated_members_imported_before_it` (child
  interpreter, import before `activate()`): asserts local events armed
  on `plain`, `cached.__wrapped__`, `decorated.__wrapped__`,
  `cm.__wrapped__`, a dict-held and a list-held lambda; calls all of
  them in a test context and asserts the node records
  `('pkgx/late.py','plain',4)`, `('cached',7)`, `('decorated',17)`,
  `('cm',21)` (`co_firstlineno` is the decorator line for decorated
  defs — identical objects to what the audit path arms) plus the
  module entry from the lambdas (`<lambda>` normalizes to None).
- Stash proof: with only `selection_recorder.py` stashed, the new test
  fails at the lru_cache arming assert while the old
  `test_activation_arms_modules_imported_before_it` still passes
  (confirming the old test did not cover the gap); with the fix, all
  pass. No test was weakened, skipped, or deleted.

## Verification (all `ptest` from the T4 worktree root)

- `ptest tests/ng/test_selection_recorder.py` → 29 passed, 3 skipped
  (exit 0; skips are the pre-existing barrier/ingest gates).
- `ptest tests/ng/test_selection_recorder.py
  tests/ng/test_selection_bridge_subprocess.py
  tests/ng/test_pytest_bridge_unit.py` → 71 passed, 6 skipped
  (exit 0).

## Notes

- Pass-1 observation: the first version of the new test expected
  `def`-line numbers for decorated functions; a `/tmp` probe (not
  committed) showed `co_firstlineno` is the decorator line on
  3.14.7, so expectations use 7/17/21 — the same code objects either
  walk arms, so import-timing parity holds.
- No push, no merge, no deploy, no live-database contact. Staged
  explicit paths only in the T4 worktree; this `.pipeline/` append is
  left uncommitted in the chain worktree.
