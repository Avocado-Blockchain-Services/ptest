# T2 report — Config and contracts: stall_timeout key, manifest field, post-test-stall reason code

- taskWorktree: /home/ingmar/worktrees/ptest/cc-post-test-stall/ptest-T2
- taskBranch: feature/post-test-stall-T2
- commit: 11b759f ("T2: stall_timeout config key, stall contracts, post-test-stall reason code", parent 3ab75e8)
- status: DONE (all acceptance criteria met; no BLOCKED)

## What was built

`src/ptest/contracts.py` — shared barrier content (design §9, edits A–J applied verbatim; the
barrier was present in neither the chain nor the task worktree, so T2 applied it as owner of record):
constants DEFAULT_STALL_TIMEOUT_S=120.0, MIN_STALL_TIMEOUT_S=10.0, MAX_STALL_TIMEOUT_S=86400.0,
STALL_DUMP_WAIT_S=1.0, STALL_MARKER_SUFFIX='.done', STACK_DUMP_INFIX='.stack-',
STACK_DUMP_HEADER_PREFIX='ptest stack dump: '; "post-test-stall" in REASON_CODES;
RunnerConfig.stall_timeout_s (None=absent, 0=disabled, else 0 or 10..86400, repr=False);
stall_marker_path / stack_dump_path (ValueError on pid<=0 or non-int, bool rejected) /
stack_dump_pid (1–10 ASCII digits, no leading zero, own-report prefix);
LaunchManifest.stall_timeout_s (None or 0.1..86400), always encoded as key "stall_timeout_s",
decoded via obj.get; GUARD_PROTOCOL_VERSION unchanged (2).

`src/ptest/config.py` — `_runner` accepts `stall_timeout` via new `_optional_stall_timeout`
(0 or 10..86400; bool/string/non-finite/negative/1..9.99/>86400 → `_fail()` = invalid-config,
exit 2); passed as `RunnerConfig(stall_timeout_s=...)`; render emits `stall_timeout = {v:g}`
after `full_timeout` only when set (`0` renders as `stall_timeout = 0`). No CLI flag.

Tests (strict TDD — 37 new tests failed pre-change, all pass post-change):
`tests/ng/test_contracts.py` — constants, reason-code registration (Reason+Problem validate),
RunnerConfig bounds (None/0/10/10.5/120/86400 accept; -1/5/9.99/"120"/1e9/true/nan/inf/86400.5
reject), repr omission, LaunchManifest round-trip (None/1.0/120.0, key present in JSON body),
decode+constructor rejection (0/-1/1e9/"x"/true → protocol-mismatch / ValueError), path-helper
positive/negative cases (foreign report name, "0123", 11 digits, empty suffix, non-ASCII digits,
trailing junk, bare name, empty).
`tests/ng/test_config.py` — absent→None; 0/10/10.5/120/86400 parse; -1/5/9.99/"120"/1e9/true/
nan/inf/86400.5 → invalid-config; render line order after full_timeout, zero rendering,
render↔parse round-trip.

`docs/schemas/v1/run.json` — unchanged: it enumerates no reason codes (reason `code` fields are
plain `{"type": "string"}`) and no runner timeouts. `uv run scripts/export-schemas.py --check`
exits 0, proving no drift. No migration generated (none exists in this repo).

## Verification (all from the task worktree root, runner `ptest`)

- `ptest tests/ng/test_contracts.py tests/ng/test_config.py` → 350 passed (313 existing + 37 new).
  Pre-change run of the same scope: 37 failed (the new tests), 313 passed — the failing set was
  exactly the new tests.
- `ptest tests/ng/test_run_deadline.py` (T4-owned; pins EXPECTED_FRESH_TOML byte-identical render
  and timeout bounds) → 89 passed. Existing configs without the key render byte-identically.
- `ptest tests/ng/test_guard.py` (T3-owned; manifest codec consumer) → 94 passed.
- `uv run scripts/export-schemas.py --check` → exit 0, no drift output.
- Note: `ptest` prints `unknown-input: the scoped run ran on a dirty source tree` because the
  worktree has uncommitted-by-others? No — at run time the tree held my own uncommitted edits;
  tree is committed now. This line is informational, not a failure (all suites report passed).

## Acceptance-criteria tick-off (design §T2)

- Parse 0/10/120/86400/10.5 accept; -1/5/9.99/"120"/1e9/true/nan/inf/86400.5 invalid-config (exit 2
  via existing `_fail`): MET (`src/ptest/config.py:357`, tests `test_config.py` stall section).
- RunnerConfig None when absent; DEFAULT 120.0; render only when set, like timeout: MET.
- LaunchManifest None/0.1..86400 validation + encode/decode round-trip: MET.
- "post-test-stall" in REASON_CODES; no schema enum to regenerate: MET.
- Existing key-less configs parse/render byte-identically: MET (89 deadline tests green).
- No test pinning exact manifest JSON keys exists; new tests assert `stall_timeout_s` present: MET.

## Smell sweep (dan-jefferies pass 3, condensed)

- Bugs: `_optional_stall_timeout` rejects bool before int check (bool is int subclass) — mirrors
  `_optional_timeout`; RunnerConfig double-validates via constructor (config passes floats;
  constructor re-checks; both agree). `-0.0` parses as disabled 0 and re-renders as `-0`, which
  re-parses identically — degenerate but stable, not user-reachable via TOML `0`.
- Duplicates: no existing stall/timeout helper covered 0-or-range; `_optional_timeout` (1..86400)
  could not express the 0 exception — new helper justified.
- Contract drift: `full_timeout_s` consumers grepped (`operations.py:146,189` compound-timeout
  resolution — T4-owned, untouched); stall seam there belongs to T4 per design §2.4.
- Tests are non-vacuous: pre-change run showed exactly the 37 new tests failing; invalid-config
  tests passed pre-change via unknown-key, but post-change the key is known and valid values
  parse, so rejection is provably value-based.
- Owned-file set == touched-file set (config.py, contracts.py, test_config.py, test_contracts.py).
  run.json untouched with schema-check evidence.

## Integration notes / seams for the chain

- The shared contracts barrier (design §0/§9) was NOT pre-applied on the chain base; T2 applied
  edits A–J verbatim in commit 11b759f. Whoever integrates T1/T3/T4 must take contracts.py from
  this branch (or cherry-pick 11b759f's contracts.py hunk) — T3/T4 construct
  `LaunchManifest(stall_timeout_s=...)` and `Problem("post-test-stall")` and cannot work without it.
- T4 owns `operations._stall_timeout_s` mapping (absent→120, 0→None, non-pytest→None) and all
  guard/bridge/printing work; T2 deliberately added no operations/guard code.
- Confidence: high. Implemented: everything above. Verified: suites listed above, all observed
  passing in-session. Not verified: `ptest --full` (explicitly owned by the integrator).
  Deferred: none. Discovered-but-not-fixed: none (pre-existing >100-char lines in touched files
  left as-is).
