# Vitest native qualification matrix

No real Node/Vitest/Vite tuple is qualified in this foundation. The only
automated bridge coverage uses `stub-node.mjs`, a handwritten deterministic API
double; it is not a native Vitest qualification and it never installs fixtures.

The prepared contract permits only `runner.kind=vitest`, explicit scoped plans,
one serial slot, and the `basic_serial` label. Its capability remains
`unavailable`: the executor has not yet allocated and consumed a private report
or completed the guarded lifecycle. A missing report allocation fails closed
before project loading.

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
