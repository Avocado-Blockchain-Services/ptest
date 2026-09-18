# Vitest native qualification matrix

No real Node/Vitest/Vite tuple is qualified in this foundation. The only
automated bridge coverage uses `stub-node.mjs`, a handwritten deterministic API
double; it is not a native Vitest qualification and it never installs fixtures.

The prepared contract permits only `runner.kind=vitest`, explicit scoped plans,
one serial slot, and the `basic_serial` label. Its capability remains
`unavailable`: the executor has not yet allocated and consumed a private report
or completed the guarded lifecycle. A missing report allocation fails closed
before project loading.

Preparation binds `plan.files` independently as `PTEST_VITEST_SCOPED_FILES`, a
UTF-8 JSON string array with 1–256 entries and at most the shared 65,536-byte
control-frame bound. The existing prepared argv limits also apply. Empty,
option-shaped, NUL-containing, and invalid-Unicode file values refuse; safe
literal values are preserved. Before loading the project, the bridge verifies
that this immutable binding matches the argv suffix. Before creating Vitest it
requires the parsed filter to match the binding exactly in values and order.
`start` receives the frozen binding, so a mutable parser result cannot broaden
the request during creation. Empty, swallowed, extra, reordered, and mismatched
filters produce an incomplete `bridge-refused` report with exit 70, null native
exit, and no test execution.

Raw and parsed configuration/root/directory, changed/related, standalone, shard,
and API overrides refuse. Resolved configurations also refuse those active
controls and every API object, including middleware-only objects. The double
retains the recorded Vitest 3.2.x middleware API default, but successful boundary
tests explicitly use `api: false`. They therefore do not establish that real
Vitest can reach the successful path under this policy.

Production wiring remains deferred: `selection.choose_plan` and
`operations._plan` currently create scoped plans with empty `Plan.files`, while
`operations._effective_config` folds request argv into configured runner args.
This foundation must not be wired into execution until a later lifecycle task
supplies the independently bound scoped suffix and executor-owned report flow.

The eventual executor-owned terminal record has exactly the shared
`NativeTerminalReport` fields: `protocol`, `run_id`, `nonce`, `attempt_id`,
`runner`, `observed_runtime_version`, `execution_mode`, `effective_profile`,
`terminal_complete`, `native_exit_code`, `bridge_exit_code`, and `problem`.
It contains no event/profile/status/files/all-tests-run compatibility record.

A later, preprovisioned real subprocess task must record the exact OS, Node,
Vitest, and Vite versions plus pass, failure, cancellation, report replay, and
same-checkout serialization evidence before changing this matrix or advertising
any supported tuple. Project config and hooks are trusted local code, not a
sandbox; the bridge refuses incompatible execution controls but does not claim
to contain their side effects.
