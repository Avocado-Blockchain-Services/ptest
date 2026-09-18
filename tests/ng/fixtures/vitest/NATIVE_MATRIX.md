# Task 11 acceptance boundary

Task 6 executes only `stub-node.mjs`, a handwritten API double copied into a
temporary fake package. No native Vitest install or execution has occurred.
The checked-in package-lock files remain unchanged and no fixture contains
node_modules. Candidate ptest owns setup and every native invocation below.

Before execution Task 11 allocates an exclusive report destination under its
private run/domain directory, outside the checkout. It binds both
`PreparedRun.report_path` and `PTEST_VITEST_REPORT_PATH` to that destination.
Preparation has no domain/allocator argument, so it leaves report_path unset
and capability unavailable. Missing binding fails before project module loading.
The bridge creates one 0600 JSON-lines terminal artifact without overwriting
anything. `complete` means observed terminal plus clean bridge/close lifecycle;
`status` and `exit_code` distinguish passed, failed, and incomplete. A fully
observed test/coverage failure can be complete and failed. All emitted events
remain nonselecting and baseline-ineligible; they are not advanced identity
telemetry or proof of process-group quiescence.

Run the advanced 3.2.7 and each basic version pair through candidate ptest. Pin
Node 26.8.2 for the initial matrix and record Linux/macOS evidence separately.
The bridge's 3.2.7 path uses public exact specifications; older serial tuples
use native start without prior init. Task 11 must qualify the 3.2.7 serial
fallback policy separately before advertising that tier.

Use `--config adversarial.config.mjs` with each `PTEST_NATIVE_CASE` name in the
advanced fixture. Expected pre-test rejection cases: pool-match-threads,
pool-match-custom, typecheck, watch, api, browser, workspace, project-threads,
foreign-hook, spoofed-hook, reporter-removal. In full mode also reject
name-filter, shard, bail and CLI forms of testNamePattern, changed, related,
project, shard, bail, positional filters and owned worker controls. The
coverage-failure case must preserve the native failed exit and emit failed
terminal evidence only after coverage reporting. Verify matching and mismatched
coverage-v8 tuples, missing Vitest, and versions outside the finite table.

Additional required native assertions: exact module identity for alpha versus
alphabet and duplicate titles; scoped native substring filters and literal
metacharacters; custom/default reporter preservation; observed worker and
concurrent-test limits including effective project overrides and inherited
environment; snapshot/config mutation makes final input evidence ineligible;
close exceptions, delayed coverage failure and hanging teardown; absence of
API/browser/watch startup; no baseline or automatic selection promotion from
this foundation. Setup must disclose the lockfiles' lifecycle scripts.

The stub suite verifies bridge decisions, argv and artifact behavior. It cannot
establish Vitest compatibility, actual pool concurrency, coverage parity,
workspace/plugin initialization timing, subprocess containment or native IDs.
